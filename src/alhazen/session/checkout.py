"""The written record of a pre-session checkout.

``check-rig`` answers one question — may this session start — and then the
answer is gone, because it only ever existed as scrollback in whichever
terminal was open at the rig. This module keeps the other half: what each
device *did* on the day. The reward pulse that was commanded and the one that
was measured, every sync line by name with what was sent down it, what the
recorder handed back, how long the tracker took to answer, the lag the sorter
was running at — plus the rig file, the alhazen revision, and when.

Why that is worth a file. Nothing in a pass/fail line survives to be compared:
a rig that has been degrading for a fortnight passes every check on the
morning it finally breaks, and the number that had been drifting was on a
screen nobody kept. Two records, a week apart, show it. So the record is
written on **every** run, passing or failing, and the failing one matters
more — it says how far each device got before it stopped.

**Two files, not one.** ``checkout.json`` is the record: JSON, because the
thing that reads it back is a program comparing today against last Tuesday,
and ``sort_keys=True`` makes two runs diffable line by line. ``checkout.txt``
beside it is a rendering of exactly the same object for whoever is standing at
the rig with a wiring diagram. Choosing one would have meant regretting it:
a YAML-only record invites hand-editing, and a text-only record cannot be
compared by anything but a person's memory.

Nothing here decides a pass. ``check_rig`` alone does that, and this module
writes down what it saw; a record that could fail a rig would be a second,
quieter gate in a file nobody reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alhazen.config.snapshot import build_provenance
from alhazen.session.checks import CheckResult

# Bumped when the shape below changes in a way a reader must know about. A
# comparison across a schema bump is the one comparison that can be wrong
# without looking wrong, so the version travels with the record.
CHECKOUT_SCHEMA_VERSION = 1

SUMMARY_SUFFIX = ".txt"


@dataclass(frozen=True)
class CheckoutRecord:
    """One run of ``check-rig``, with its evidence, ready to be written."""

    rig_file: str
    pulse: bool
    results: tuple[CheckResult, ...]
    provenance: dict[str, str]
    created: str

    @property
    def ok(self) -> bool:
        """Exactly what the console reported — recomputed from the same
        results, never stored separately, so the record cannot disagree with
        the exit code of the run that produced it."""
        return all(result.ok for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKOUT_SCHEMA_VERSION,
            "created": self.created,
            "rig_file": self.rig_file,
            "pulse": self.pulse,
            "ok": self.ok,
            "provenance": self.provenance,
            "devices": {
                result.name: {
                    "ok": result.ok,
                    "detail": result.detail,
                    "evidence": result.evidence,
                }
                for result in self.results
            },
            # Said in the record for the same reason it is said on the
            # console: a file that lists eight OK devices and nothing else
            # reads like a rig that was fully verified.
            "untested": ["display (verifying it means opening a window, which is a session)"],
        }

    def render(self) -> str:
        """The human-readable summary written beside the JSON."""
        lines = [
            f"pre-session checkout — {'PASS' if self.ok else 'FAIL'}",
            f"  when: {self.created}",
            f"  rig file: {self.rig_file}",
            f"  alhazen: {self.provenance.get('alhazen_version', '?')} "
            f"({self.provenance.get('alhazen_git_describe', '?')})",
            f"  pulse: {'yes — reward and every mapped sync line fired' if self.pulse else 'no'}",
        ]
        for result in self.results:
            lines.append(f"  {'OK  ' if result.ok else 'FAIL'} {result.name}: {result.detail}")
            lines.extend(f"       {line}" for line in _render_evidence(result))
        lines.append("       display: untested (needs a real session)")
        return "\n".join(lines)

    def write(self, path: Path | str) -> tuple[Path, Path]:
        """Write the record and its summary; return both paths.

        The caller names the JSON path and the summary takes its place with a
        ``.txt`` suffix, so one ``--record`` option cannot produce a pair that
        sits in two different directories. A path that is not named ``.json``
        gets the suffix appended rather than replaced: dated names like
        ``checkout.2026-09-12`` would otherwise all collapse onto one summary
        file and overwrite each other.
        """
        record_path = Path(path)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        summary_path = (
            record_path.with_suffix(SUMMARY_SUFFIX)
            if record_path.suffix == ".json"
            else record_path.with_name(record_path.name + SUMMARY_SUFFIX)
        )
        summary_path.write_text(self.render() + "\n", encoding="utf-8")
        return record_path, summary_path


def build_record(rig_file: Path | str, results: list[CheckResult], pulse: bool) -> CheckoutRecord:
    """Assemble the record from a finished ``check_rig`` run.

    Provenance comes from the same :func:`build_provenance` every session
    snapshot uses, so "which alhazen was this rig checked out with" is
    answered in the same words in both places — including the git describe,
    because between releases the version string does not identify the code.
    """
    return CheckoutRecord(
        rig_file=str(rig_file),
        pulse=pulse,
        results=tuple(results),
        provenance=build_provenance(),
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def read_record(path: Path | str) -> dict[str, Any]:
    """Read a record back. Plain dicts on purpose — the thing reading last
    month's checkout may be a newer alhazen whose dataclass has moved on."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def differences(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """What changed between two checkouts of the same rig, one line each.

    This is why the record exists: not to be archived, but to be held up
    against the last one. Every leaf of every device's evidence is compared,
    so a lag that doubled or a sync line that quietly lost an event shows up
    without anyone knowing in advance which number to watch.

    Measurements are compared as written. Two runs of the same healthy rig
    will differ in the measured milliseconds, and that is the point: the
    reader decides what a meaningful drift is, because only the reader knows
    what their DAQ jitter looks like. This function reports; it judges
    nothing and gates nothing.
    """
    if before.get("schema_version") != after.get("schema_version"):
        return [
            f"schema_version: {before.get('schema_version')} -> {after.get('schema_version')} "
            f"(records from different schema versions are not comparable leaf by leaf)"
        ]
    out: list[str] = []
    for key in ("rig_file", "pulse", "ok"):
        if before.get(key) != after.get(key):
            out.append(f"{key}: {before.get(key)!r} -> {after.get(key)!r}")
    for key in sorted(set(before.get("provenance", {})) | set(after.get("provenance", {}))):
        # environment_digest is a fingerprint of every installed package; it
        # moves whenever anything is pip-installed on the rig machine, which
        # is exactly the kind of change worth seeing beside a number that
        # also moved.
        old, new = before.get("provenance", {}).get(key), after.get("provenance", {}).get(key)
        if old != new and key != "created":
            out.append(f"provenance.{key}: {old!r} -> {new!r}")
    old_devices = before.get("devices", {})
    new_devices = after.get("devices", {})
    for name in sorted(set(old_devices) | set(new_devices)):
        if name not in old_devices:
            out.append(f"{name}: not in the earlier record")
            continue
        if name not in new_devices:
            out.append(f"{name}: no longer checked")
            continue
        out.extend(
            f"{name}.{path}: {old!r} -> {new!r}"
            for path, old, new in _leaf_differences(old_devices[name], new_devices[name])
        )
    return out


def _leaf_differences(before: Any, after: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Every differing leaf of two nested structures, by dotted path."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[tuple[str, Any, Any]] = []
        for key in sorted(set(before) | set(after)):
            here = f"{path}.{key}" if path else str(key)
            out.extend(_leaf_differences(before.get(key), after.get(key), here))
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        out = []
        for index, (old, new) in enumerate(zip(before, after, strict=True)):
            out.extend(_leaf_differences(old, new, f"{path}[{index}]"))
        return out
    return [] if before == after else [(path, before, after)]


def _render_evidence(result: CheckResult) -> list[str]:
    """The measurements worth reading out loud, per device.

    Hand-written per device rather than dumped generically: this is the page
    somebody reads standing at the rig, and the two numbers that matter for
    reward (commanded, measured) must not be buried in fifteen that do not.
    Everything, including what is not shown here, is in the JSON.
    """
    e = result.evidence
    if not e or e.get("configured") is False:
        return []
    if result.name == "reward" and e.get("pulsed"):
        measured = e.get("measured_ms")
        note = " — simulated: nothing was played out" if e.get("simulated") else ""
        return [
            f"pulse commanded {e.get('commanded_ms')} ms, measured "
            f"{'?' if measured is None else measured} ms on "
            f"{e.get('device')}/{e.get('channel')} at {e.get('voltage')} V{note}"
        ]
    if result.name == "sync":
        out = []
        for line in e.get("lines", []):
            events = ", ".join(line["events"]) or "no events mapped"
            if line["pulsed"]:
                sent = (
                    f"sent {line['commanded_ms']} ms, measured "
                    f"{'?' if line['measured_ms'] is None else line['measured_ms']} ms"
                )
            else:
                sent = "nothing sent (no --pulse)"
            out.append(f"{line['line']}: {sent} — carries {events}")
        return out
    if result.name == "recording":
        returned = e.get("returned")
        said = "nothing wrong" if returned is None else returned
        return [f"{e.get('data_dir')} (exists: {e.get('exists')}) — recorder said: {said}"]
    if result.name == "spikes" and e.get("backend") == "sorted_stream":
        lag = "unknown" if e.get("lag_ms") is None else f"{e['lag_ms']} ms"
        return [
            f"{e.get('address')}: listened {e.get('listened_s')} s, publishing="
            f"{e.get('publishing')}, units announced={e.get('units_announced')}, "
            f"units={e.get('units')}, lag={lag}, dropped={e.get('dropped_messages')}"
        ]
    if result.name == "eyetracker" and e.get("connect_ms") is not None:
        return [f"answered in {e['connect_ms']} ms"]
    return []


__all__ = [
    "CHECKOUT_SCHEMA_VERSION",
    "CheckoutRecord",
    "build_record",
    "differences",
    "read_record",
]
