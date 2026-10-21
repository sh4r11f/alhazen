"""Machine rig configs on the platform: the record each host keeps per machine, and the rules
that change it (docs/design/machine-rig-configs.md §4).

A rig's dashboard keeps several named rig configs (cli/machine_rig.py) and syncs them with
its owner's platform over the library connection. The platform keeps, per person and per
machine, one **machine record**: the configs as the platform last knew them, each with a
revision counter that moves on every content write from either side, plus what the machine
reported it runs. The machine is the validator: a platform edit is ``pending`` until the
machine takes it (``synced``) or refuses it (``rejected``, with the reason).

Every write is compare-and-set on the config's revision, so neither side overwrites a change
it has not seen: a machine write against a revision the platform moved past is not applied,
and the machine reports a conflict instead (kept beside the platform's text until the person
keeps one of the two, on either side).

This module is pure (stdlib only, no I/O) so that every host applies the same rules: the
standalone hosts through ``RigStore`` (rigstore.py), and the Mana host's ``alhazen-library``
service, which vendors this file. Records are plain JSON-able dicts in the shape of the
contract; times are milliseconds since the epoch, passed in.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

SCHEMA = 1
MAX_TEXT = 256 * 1024
MAX_CONFIGS = 64
MAX_TEMPLATES = 2 * 1024 * 1024
MAX_SCHEMA = 1024 * 1024
MAX_REASON = 2000
MACHINE_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
CONFIG_ID = re.compile(r"^[A-Za-z0-9_-]{2,40}$")
# alhazen.config.rigs.NAME_PATTERN, copied so this module stays standalone.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MAX_NAME = 64
STATES = ("synced", "pending", "rejected")

Record = dict[str, Any]


class RigSyncError(ValueError):
    """A refusal, worded for the person. ``kind`` maps onto the host's error classes:
    ``invalid`` (400), ``conflict`` (409), ``missing`` (404)."""

    def __init__(self, message: str, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_machine_id(mid: Any) -> str:
    if not isinstance(mid, str) or not MACHINE_ID.match(mid):
        raise RigSyncError("A machine id is 6 to 64 letters, digits, '_' or '-'")
    return mid


def check_config_id(cid: Any) -> str:
    if not isinstance(cid, str) or not CONFIG_ID.match(cid):
        raise RigSyncError("A rig config id is 2 to 40 letters, digits, '_' or '-'")
    return cid


def check_name(name: Any) -> str:
    if not isinstance(name, str) or len(name) > MAX_NAME or not NAME.fullmatch(name.strip()):
        raise RigSyncError(
            "A rig config's name is letters, digits, '.', '_' or '-', starting with a letter "
            "or digit, at most 64 characters (it becomes the rig's file name, rig-<name>.yaml)"
        )
    return name.strip()


def check_text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise RigSyncError("A rig config's text is empty")
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise RigSyncError("A rig config must be at most 256 KiB")
    return text


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RigSyncError(f"{what} must be a whole number of at least 0")
    return value


def new_record(mid: str) -> Record:
    return {
        "schema": SCHEMA,
        "id": check_machine_id(mid),
        "label": None,
        "alhazen": None,
        "seenAt": None,
        "token": None,
        "default": None,
        "defaultRev": 0,
        "defaultBy": None,
        "rigSchema": None,
        "rigSchemaSha": None,
        "templates": None,
        "configs": {},
    }


def _blank_config(cid: str, name: str) -> Record:
    return {
        "id": cid,
        "name": name,
        "text": "",
        "sha256": None,
        "revision": 0,
        "state": "synced",
        "deleted": False,
        "updatedAt": None,
        "updatedBy": None,
        "basedOn": None,
        "running": None,
        "rejection": None,
        "conflict": None,
        "calibration": None,
    }


def normalize(record: Record | None, mid: str) -> Record:
    """``record`` with every field of the contract present, as a fresh copy. A store that
    drops empty values (Firebase drops ``{}``, ``[]`` and ``null``) gives back records
    with fields missing; this puts them back."""
    out = new_record(mid)
    if isinstance(record, dict):
        for key in out:
            if key != "configs" and key in record:
                out[key] = copy.deepcopy(record[key])
        for cid, config in (record.get("configs") or {}).items():
            if not isinstance(config, dict):
                continue
            full = _blank_config(str(cid), str(config.get("name") or cid))
            for key in full:
                if key in config:
                    full[key] = copy.deepcopy(config[key])
            full["id"] = str(cid)
            full["deleted"] = bool(full["deleted"])
            if full["state"] not in STATES:
                full["state"] = "synced"
            out["configs"][str(cid)] = full
    out["id"] = mid
    out["defaultRev"] = out["defaultRev"] if isinstance(out["defaultRev"], int) else 0
    return out


def _live(record: Record) -> dict[str, Record]:
    return {cid: c for cid, c in record["configs"].items() if not c["deleted"]}


def _name_taken(record: Record, name: str, cid: str) -> bool:
    return any(c["name"] == name and other != cid for other, c in _live(record).items())


# ---------------------------------------------------------------------- the machine's side
def apply_push(
    record: Record | None, mid: str, push: Any, now: int, *, token: str | None = None
) -> tuple[Record, dict[str, Any]]:
    """Apply one sync from the machine ``mid`` (the contract's ``Push``) to its record, and
    return the new record and the per-config results. Everything in ``push`` is checked
    before anything is applied, so a malformed push changes nothing."""
    if not isinstance(push, dict):
        raise RigSyncError("A sync is a JSON object")
    rec = normalize(record, check_machine_id(mid))
    writes = _list(push, "writes")
    acks = _list(push, "acks")
    conflicts = _list(push, "conflicts")
    deletes = _list(push, "deletes")
    running = push.get("running") or {}
    if not isinstance(running, dict):
        raise RigSyncError("running must be an object")
    for w in writes:
        check_config_id(w.get("id"))
        check_name(w.get("name"))
        check_text(w.get("text"))
        _int(w.get("base"), "A write's base revision")
    for a in acks:
        check_config_id(a.get("id"))
        _int(a.get("revision"), "An ack's revision")
        if not isinstance(a.get("ok"), bool):
            raise RigSyncError("An ack says ok: true or false")
    for c in conflicts:
        check_config_id(c.get("id"))
        check_text(c.get("text"))
    for d in deletes:
        check_config_id(d.get("id"))
        _int(d.get("base"), "A deletion's base revision")
    for cid, report in running.items():
        check_config_id(cid)
        if not isinstance(report, dict):
            raise RigSyncError("Each running report is an object")
        check_text(report.get("text"))
    schema = push.get("rigSchema")
    if schema is not None and (not isinstance(schema, str) or len(schema) > MAX_SCHEMA):
        raise RigSyncError("rigSchema is JSON text of at most 1 MiB")
    templates = push.get("templates")
    if templates is not None and (not isinstance(templates, str) or len(templates) > MAX_TEMPLATES):
        raise RigSyncError("templates is JSON text of at most 2 MiB")

    results: dict[str, Any] = {}
    for w in writes:
        cid = w["id"]
        current = rec["configs"].get(cid)
        revision = current["revision"] if current else 0
        if w["base"] != revision:
            results[cid] = {"ok": False, "conflict": True, "revision": revision}
            continue
        name = check_name(w["name"])
        if _name_taken(rec, name, cid):
            results[cid] = {
                "ok": False,
                "conflict": True,
                "revision": revision,
                "reason": f"another config on this machine is named {name!r}",
            }
            continue
        if current is None:
            if len(_live(rec)) >= MAX_CONFIGS:
                raise RigSyncError(f"A machine keeps at most {MAX_CONFIGS} rig configs")
            current = rec["configs"][cid] = _blank_config(cid, name)
        current.update(
            name=name,
            text=w["text"],
            sha256=sha256_text(w["text"]),
            revision=revision + 1,
            state="synced",
            deleted=False,
            updatedAt=now,
            updatedBy="machine",
            basedOn=None,
            rejection=None,
            conflict=None,
        )
        results[cid] = {"ok": True, "revision": current["revision"]}
    for a in acks:
        current = rec["configs"].get(a["id"])
        if current is None or current["revision"] != a["revision"]:
            continue  # stale: the config moved on (or went) since the machine read it
        if current["deleted"]:
            if a["ok"]:
                del rec["configs"][a["id"]]
            else:
                current.update(
                    deleted=False,
                    state="rejected",
                    rejection=_rejection(a, now),
                    text=current["text"],
                )
            continue
        if a["ok"]:
            current.update(state="synced", rejection=None, conflict=None)
        else:
            current.update(state="rejected", rejection=_rejection(a, now))
    for c in conflicts:
        current = rec["configs"].get(c["id"])
        if current is None:
            continue
        current["conflict"] = {"text": c["text"], "sha256": sha256_text(c["text"]), "at": now}
    for d in deletes:
        current = rec["configs"].get(d["id"])
        if current is None:
            results[d["id"]] = {"ok": True, "revision": 0}
        elif current["revision"] == d["base"]:
            del rec["configs"][d["id"]]
            results[d["id"]] = {"ok": True, "revision": 0}
        else:
            results[d["id"]] = {"ok": False, "conflict": True, "revision": current["revision"]}
    for cid, report in running.items():
        current = rec["configs"].get(cid)
        if current is None:
            continue
        current["running"] = {
            "name": str(report.get("name") or current["name"])[:MAX_NAME],
            "text": report["text"],
            "sha256": sha256_text(report["text"]),
            "problem": _short(report.get("problem")),
            "at": now,
        }
        calibration = report.get("calibration")
        current["calibration"] = calibration if isinstance(calibration, dict) else None
    default = push.get("default")
    seen = push.get("defaultRevSeen", 0)
    if (
        isinstance(default, str)
        and isinstance(seen, int)
        and seen == rec["defaultRev"]
        and default in _live(rec)
        and default != rec["default"]
    ):
        rec.update(default=default, defaultRev=rec["defaultRev"] + 1, defaultBy="machine")
    for key in ("label", "alhazen"):
        if isinstance(push.get(key), str):
            rec[key] = push[key][:80]
    if schema is not None:
        rec["rigSchema"] = schema
        rec["rigSchemaSha"] = push.get("rigSchemaSha") or sha256_text(schema)
    if templates is not None:
        rec["templates"] = templates
    rec["seenAt"] = now
    if token is not None:
        rec["token"] = token
    return rec, results


def _list(push: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = push.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise RigSyncError(f"{key} must be a list of objects")
    return value


def _short(text: Any) -> str | None:
    return None if text in (None, "") else str(text)[:MAX_REASON]


def _rejection(ack: dict[str, Any], now: int) -> dict[str, Any]:
    return {
        "revision": ack["revision"],
        "reason": _short(ack.get("reason")) or "refused",
        "at": now,
    }


# ---------------------------------------------------------------------- the owner's side
def _config(rec: Record, cid: str) -> Record:
    config = rec["configs"].get(check_config_id(cid))
    if config is None or config["deleted"]:
        raise RigSyncError("This machine has no such rig config", "missing")
    return config


def owner_put(
    record: Record | None,
    mid: str,
    cid: str,
    *,
    name: Any,
    text: Any,
    base: Any,
    based_on: Any,
    now: int,
) -> Record:
    """A config created or changed on the platform: a new pending revision for the machine
    to take. Refused (``conflict``) when ``base`` is not the current revision."""
    if record is None:
        raise RigSyncError("This machine has not synced with the platform yet", "missing")
    rec = normalize(record, mid)
    check_config_id(cid)
    name = check_name(name)
    text = check_text(text)
    base = _int(base, "base")
    current = rec["configs"].get(cid)
    if current is not None and current["deleted"]:
        raise RigSyncError("This rig config is being deleted", "conflict")
    revision = current["revision"] if current else 0
    if base != revision:
        raise RigSyncError(
            "This rig config changed since you opened it; reopen it and make the change again",
            "conflict",
        )
    if _name_taken(rec, name, cid):
        raise RigSyncError(f"This machine already has a rig config named {name!r}")
    if current is None:
        if len(_live(rec)) >= MAX_CONFIGS:
            raise RigSyncError(f"A machine keeps at most {MAX_CONFIGS} rig configs")
        current = rec["configs"][cid] = _blank_config(cid, name)
    current.update(
        name=name,
        text=text,
        sha256=sha256_text(text),
        revision=revision + 1,
        state="pending",
        updatedAt=now,
        updatedBy="platform",
        basedOn=based_on if isinstance(based_on, str) and based_on else current.get("sha256"),
        rejection=None,
    )
    return rec


def owner_delete(record: Record | None, mid: str, cid: str, *, base: Any, now: int) -> Record:
    """A deletion asked on the platform: pending until the machine removes the config. A
    config the machine never ran is dropped at once; the default config is refused."""
    if record is None:
        raise RigSyncError("This machine has not synced with the platform yet", "missing")
    rec = normalize(record, mid)
    current = _config(rec, cid)
    if _int(base, "base") != current["revision"]:
        raise RigSyncError(
            "This rig config changed since you opened it; reopen it before deleting it", "conflict"
        )
    if rec["default"] == cid:
        raise RigSyncError(
            "This is the machine's default rig config: make another the default first"
        )
    if current["running"] is None:
        del rec["configs"][cid]
        return rec
    current.update(
        deleted=True,
        state="pending",
        revision=current["revision"] + 1,
        updatedAt=now,
        updatedBy="platform",
        rejection=None,
    )
    return rec


def owner_default(record: Record | None, mid: str, cid: str, *, base: Any, now: int) -> Record:
    if record is None:
        raise RigSyncError("This machine has not synced with the platform yet", "missing")
    rec = normalize(record, mid)
    _config(rec, cid)
    if _int(base, "base") != rec["defaultRev"]:
        raise RigSyncError("The default changed since you opened this page; reload it", "conflict")
    if rec["default"] != cid:
        rec.update(default=cid, defaultRev=rec["defaultRev"] + 1, defaultBy="platform")
    return rec


def owner_resolve(record: Record | None, mid: str, cid: str, *, keep: Any, now: int) -> Record:
    """Settle a conflict on the platform. ``keep`` is "machine" (the machine's version
    becomes the platform's) or "platform" (the platform's stays). Either way a new pending
    revision is written whose ``basedOn`` is the machine's version, which tells the machine
    the person chose knowing it, so the machine takes it."""
    if record is None:
        raise RigSyncError("This machine has not synced with the platform yet", "missing")
    rec = normalize(record, mid)
    current = _config(rec, cid)
    conflict = current["conflict"]
    if not conflict:
        raise RigSyncError("This rig config has no conflict to settle", "conflict")
    if keep not in ("machine", "platform"):
        raise RigSyncError('Keep "machine" or "platform"')
    text = conflict["text"] if keep == "machine" else current["text"]
    current.update(
        text=text,
        sha256=sha256_text(text),
        revision=current["revision"] + 1,
        state="pending",
        updatedAt=now,
        updatedBy="platform",
        basedOn=conflict["sha256"],
        conflict=None,
        rejection=None,
    )
    return rec


# ---------------------------------------------------------------------- views
def summary(record: Record) -> Record:
    """A record for a list: no schema, no templates and no config texts."""
    out = {k: v for k, v in record.items() if k not in ("rigSchema", "templates", "configs")}
    out["hasSchema"] = bool(record.get("rigSchema"))
    out["configs"] = {
        cid: {k: v for k, v in c.items() if k not in ("text", "running", "conflict")}
        | {
            "runningSha256": (c.get("running") or {}).get("sha256"),
            "problem": (c.get("running") or {}).get("problem"),
            "hasConflict": bool(c.get("conflict")),
        }
        for cid, c in (record.get("configs") or {}).items()
    }
    return out


def to_json(record: Record) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True)
