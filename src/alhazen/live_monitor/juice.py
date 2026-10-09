"""How much juice a session delivered, trial by trial and in total.

A monkey session — an experiment session or a training stage — is paid
through the rig's reward line, and the person at the rig wants two things
from that at a glance: what each trial paid (and why: its outcome, a trial a
device cut short, a manual press of ``r``, a drop asked for mid-trial), and
how much the animal has had so far. Both are read from the session's own
event stream — the REWARD and REWARD_DELIVERED events the payer, the pause
controls and the engine write — so the plot, session.json's total and the
History details can never disagree with the record, or with the pump.

The amount is in µL when the rig has a reward calibration for every pulse
width delivered, on the line and at the voltage it ran (rig-<name>.reward.yaml,
config/reward_calibration.py); otherwise in pulses, with the valve-open time
beside it. The unit is always said.

    juice_payload(events, ul)  the live monitor's "juice" panel
    juice_totals(events, ul)   session.json's reward.delivered
    ul_by_width(cal, line, v)  µL per pulse for each measured width
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

# What a delivery was paid for, in the order the plot stacks them.
KINDS = ("outcome", "fault", "manual", "mid_trial")
KIND_LABELS = {
    "outcome": "paid for the trial's outcome",
    "fault": "paid for a trial a device cut short (on_fault)",
    "manual": "manual reward (r)",
    "mid_trial": "mid-trial drop",
}
UL = "µL"
PULSES = "pulses"


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(event.get("payload"), dict):
        return dict(event["payload"])
    try:
        found = json.loads(event.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _trial(event: Mapping[str, Any]) -> int:
    try:
        return int(float(event.get("trial_index")))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def ul_by_width(
    calibration: dict[str, Any] | None, *, line: str, voltage: float
) -> dict[int, float]:
    """µL per pulse for every width the calibration measured on this line at
    this voltage (config/reward_calibration.py ``ul_per_pulse``'s rule)."""
    from alhazen.config.reward_calibration import ul_per_pulse

    found: dict[int, float] = {}
    for width in (calibration or {}).get("widths", {}):
        entry = ul_per_pulse(calibration, pulse_ms=int(width), line=line, voltage=voltage)
        if entry and entry.get("ul_per_pulse"):
            found[int(width)] = float(entry["ul_per_pulse"])
    return found


def deliveries(events: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """Every delivery that reached the valve, in order, and the trials whose
    delivery failed.

    A mid-trial drop is counted by its REWARD_DELIVERED (its REWARD marks
    when it was commanded; a cancelled or failed one never arrived). Every
    other REWARD is emitted after the pump finished, so it is the delivery.
    """
    found: list[dict[str, Any]] = []
    failed: list[int] = []
    for event in events:
        name = event.get("event")
        if name == "REWARD_FAILED":
            failed.append(_trial(event))
            continue
        if name not in {"REWARD", "REWARD_DELIVERED"}:
            continue
        payload = _payload(event)
        if name == "REWARD" and "reason" in payload and not payload.get("manual"):
            continue  # a mid-trial drop, counted when it is delivered
        if payload.get("manual"):
            kind = "manual"
        elif name == "REWARD_DELIVERED" or "reason" in payload:
            kind = "mid_trial"
        elif "fault" in payload:
            kind = "fault"
        else:
            kind = "outcome"
        pulses = payload.get("pulses") or {}
        n = pulses.get("n_pulses")
        width = pulses.get("pulse_ms")
        found.append(
            {
                "trial_index": _trial(event),
                "kind": kind,
                "outcome": payload.get("outcome"),
                "n_pulses": int(n) if isinstance(n, (int, float)) else None,
                "pulse_ms": int(width) if isinstance(width, (int, float)) else None,
            }
        )
    return found, failed


def _unit(found: list[dict[str, Any]], ul: Mapping[int, float] | None) -> str:
    """µL when every delivery's width was measured; pulses otherwise."""
    if found and ul and all(d["pulse_ms"] in ul and d["n_pulses"] is not None for d in found):
        return UL
    return PULSES


def _amount(delivery: dict[str, Any], unit: str, ul: Mapping[int, float] | None) -> float:
    n = delivery["n_pulses"]
    if n is None:
        return math.nan
    if unit == UL:
        assert ul is not None
        return n * ul[delivery["pulse_ms"]]
    return float(n)


def juice_totals(
    events: Iterable[Mapping[str, Any]], ul: Mapping[int, float] | None = None
) -> dict[str, Any]:
    """session.json's ``reward.delivered``: what reached the valve, in total
    and by what it was paid for.

    ``volume_ul`` is null unless every delivery's width was measured
    (``unit`` says which); ``pulses`` and ``open_ms`` are always the train's
    own numbers. ``ul_per_pulse`` is the calibration the volumes used.
    """
    found, failed = deliveries(events)
    unit = _unit(found, ul)

    def tally(items: list[dict[str, Any]]) -> dict[str, Any]:
        pulses = sum(d["n_pulses"] or 0 for d in items)
        open_ms = sum((d["n_pulses"] or 0) * (d["pulse_ms"] or 0) for d in items)
        volume = round(sum(_amount(d, unit, ul) for d in items), 3) if unit == UL else None
        return {
            "deliveries": len(items),
            "pulses": pulses,
            "open_ms": open_ms,
            "volume_ul": volume,
        }

    return {
        "unit": unit,
        **tally(found),
        "trials_paid": len({d["trial_index"] for d in found if d["kind"] != "manual"}),
        "failed": len(failed),
        "by_kind": {kind: tally([d for d in found if d["kind"] == kind]) for kind in KINDS},
        "ul_per_pulse": {str(k): v for k, v in sorted((ul or {}).items())} or None,
    }


def juice_payload(
    events: Iterable[Mapping[str, Any]], ul: Mapping[int, float] | None = None
) -> dict[str, Any]:
    """The live monitor's ``juice`` form: per-trial amounts by kind, and the
    cumulative total over the session, in the unit ``juice_totals`` uses."""
    events = list(events)
    found, failed = deliveries(events)
    if not found and not failed:
        return {"form": "empty", "message": "No juice delivered yet"}
    unit = _unit(found, ul)
    per_trial: dict[int, dict[str, float]] = {}
    for delivery in found:
        amounts = per_trial.setdefault(delivery["trial_index"], dict.fromkeys(KINDS, 0.0))
        amount = _amount(delivery, unit, ul)
        amounts[delivery["kind"]] += 0.0 if math.isnan(amount) else amount
    last = max([_trial(e) for e in events] + [0])
    trials = [
        {"x": x, **{k: round(v, 3) for k, v in amounts.items()}}
        for x, amounts in sorted(per_trial.items())
    ]
    running = 0.0
    cumulative: list[list[float]] = [[max(0, (trials[0]["x"] if trials else last) - 1), 0.0]]
    for trial in trials:
        running += sum(trial[k] for k in KINDS)
        cumulative.append([trial["x"], round(running, 3)])
    if last > cumulative[-1][0]:
        cumulative.append([last, round(running, 3)])
    totals = juice_totals(events, ul)
    word = UL if unit == UL else "pulses"
    stats = [
        {
            "label": "total",
            "value": (
                f"{totals['volume_ul']:,.1f} µL" if unit == UL else f"{totals['pulses']:,} pulses"
            ),
        },
        {"label": "valve open", "value": f"{totals['open_ms'] / 1000:,.1f} s"},
        {"label": "trials paid", "value": f"{totals['trials_paid']:,}"},
    ]
    if totals["by_kind"]["fault"]["deliveries"]:
        stats.append(
            {"label": "on_fault", "value": f"{totals['by_kind']['fault']['deliveries']:,}"}
        )
    if totals["by_kind"]["manual"]["deliveries"]:
        stats.append({"label": "manual", "value": f"{totals['by_kind']['manual']['deliveries']:,}"})
    if failed:
        stats.append({"label": "failed", "value": f"{len(failed):,}", "status": "critical"})
    payload: dict[str, Any] = {
        "form": "juice",
        "unit": unit,
        "x_label": "trial",
        "y_label": f"Per trial ({word})",
        "y2_label": f"Cumulative ({word})",
        "kinds": [{"key": k, "name": KIND_LABELS[k]} for k in KINDS],
        "trials": trials,
        "cumulative": cumulative,
        "failures": sorted(set(failed)),
        "total": round(running, 3),
        "stats": stats,
    }
    if unit != UL:
        payload["note"] = (
            "In pulses: this rig has no reward calibration for every pulse width delivered "
            "(Measure rig, Reward, Juice per pulse), so no volume is claimed"
        )
    return payload
