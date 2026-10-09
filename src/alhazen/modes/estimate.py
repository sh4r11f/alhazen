"""The session-duration estimate a launch shows before it starts.

``run.py --estimate-duration`` (alhazen.cli.main) answers with what this
module puts together: the effective rig and params of the launch the same
command line would start, the task's own schedule and trial timing
(``Task.duration_schedule``, ``Task.trial_timing``; alhazen.task.duration),
the inter-trial interval the runner waits, and what the session does that no
number can promise — a calibration, rests that end when someone presses
SPACE, attempts served again after a fixation break. Those are listed beside
the number, never folded into it.

The rules, so the number is one a person can plan on:

- Only completed first attempts are counted: every planned trial once. A
  failed attempt is served again and adds up to one trial's length, which is
  said, with that length.
- A wait on the subject is counted from 0 to its cap. A configured timeout is
  the longest a wait can last, not how long it usually takes; with any such
  wait the estimate is a range and has no single expected value.
- Frame-counted durations convert at the rig file's configured refresh rate,
  and the answer says so: a session runs on the rate it measures, and refuses
  one that differs.
- Simulate mode's time is the simulated session's own, paced at that refresh
  rate; how fast this machine renders it is not estimated.

Nothing here opens a window or a device, writes a file, draws from a
session's generators or runs the params hook (which may read or write a
subject's state): a schedule is built only on a generator of its own.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from alhazen.config.models import RigConfig
from alhazen.modes import Mode
from alhazen.modes.rehearsal import shrink_params
from alhazen.modes.session import _STAND_INS, SIMULATION_REST_RESUME_S, rig_for_mode
from alhazen.paradigms.base import Condition
from alhazen.task.duration import (
    AdaptiveSchedule,
    PlannedSchedule,
    TrialTiming,
    UnknownSchedule,
    scratch_rng,
)

SCHEMA = 1
# Test and simulate turn an adaptive run down to this many trials
# (build_mode_session's default, which the command line does not change).
REHEARSAL_ADAPTIVE_TRIALS = 10


# ----------------------------------------------------------------------
# Words
# ----------------------------------------------------------------------


def duration_text(seconds: float, *, round_up: bool = False) -> str:
    """``45 s``, ``37 min``, ``2 h 05 min``: a duration as the page says it."""
    if seconds < 90:
        whole = math.ceil(seconds) if round_up else math.floor(seconds + 0.5)
        return f"{whole} s"
    minutes = math.ceil(seconds / 60) if round_up else math.floor(seconds / 60 + 0.5)
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def range_text(low: float, high: float | None, expected: float | None) -> str:
    """The headline for a timed part: ``≈ 37 min``, ``34–58 min``, or
    ``at least 34 min`` when nothing bounds it."""
    if high is None:
        return f"at least {duration_text(low)}"
    if expected is not None and math.isclose(low, high, rel_tol=0.02, abs_tol=1.0):
        return f"≈ {duration_text(expected)}"
    lo, hi = duration_text(low), duration_text(high, round_up=True)
    if lo == hi:
        return f"≈ {lo}"
    lo_num, _, lo_unit = lo.rpartition(" ")
    hi_num, _, hi_unit = hi.rpartition(" ")
    if lo_unit == hi_unit and " " not in lo_num and " " not in hi_num:
        return f"{lo_num}–{hi_num} {lo_unit}"
    return f"{lo} – {hi}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ----------------------------------------------------------------------
# Trials
# ----------------------------------------------------------------------


def _iti_s(params: Any, hz: float) -> float:
    """The params' ``iti``, as the runner waits it after every trial (0
    without one; modes/session.py reads it by name)."""
    iti = getattr(params, "iti", None)
    return float(iti.seconds(hz)) if iti is not None else 0.0


class _Timer:
    """``task.trial_timing`` per condition, asked once per distinct one."""

    def __init__(self, task: Any, hz: float) -> None:
        self._task = task
        self._hz = hz
        self._seen: dict[str, TrialTiming | None] = {}

    def __call__(self, condition: Condition) -> TrialTiming | None:
        key = repr(sorted(condition.params.items(), key=lambda kv: kv[0]))
        if key not in self._seen:
            timing = self._task.trial_timing(condition, self._hz)
            if timing is not None and not isinstance(timing, TrialTiming):
                raise TypeError(
                    f"{type(self._task).__name__}.trial_timing returned "
                    f"{type(timing).__name__}; it returns a TrialTiming or None"
                )
            self._seen[key] = timing
        return self._seen[key]


def _span_rows(timing: TrialTiming, iti_s: float) -> list[dict[str, Any]]:
    rows = [span.as_dict() for span in timing.spans]
    if iti_s > 0:
        rows.append(
            {
                "label": "inter-trial interval",
                "kind": "fixed",
                "min_s": iti_s,
                "max_s": iti_s,
                "expected_s": iti_s,
                "basis": "the params' iti, waited after every trial",
            }
        )
    return rows


def estimate_trials(
    mode: Mode,
    task: Any,
    params: Any,
    rig: RigConfig,
    *,
    trials_per_condition: int = 1,
    headless: bool = False,
    mouse: bool = False,
    rig_name: str = "",
    params_source: str | None = None,
) -> dict[str, Any]:
    """How long a run, test or simulate session of ``task`` would take.

    ``params`` are the loaded params (before the params hook); test and
    simulate reduce them the way ``build_mode_session`` does. ``rig`` is the
    rig as loaded; the mode's stand-ins (``rig_for_mode``) decide which waits
    are a person's.
    """
    if not mode.runs_trials:
        raise ValueError(f"{mode.value} runs no trials")
    hz = float(rig.monitor.refresh_rate_hz)
    effective, _notes = rig_for_mode(mode, rig, headless=headless, mouse=mouse)
    reductions: list[str] = []
    if not mode.drives_subject:
        params, changed = shrink_params(
            params,
            n_per_condition=trials_per_condition,
            max_adaptive_trials=REHEARSAL_ADAPTIVE_TRIALS,
        )
        reductions = [str(r) for r in changed]
        if changed:
            task = type(task)(params)

    answer: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": mode.value,
        "task": getattr(task, "name", type(task).__name__),
        "basis": {
            "refresh_hz": hz,
            "refresh": (
                f"frame durations at {hz:g} Hz, the refresh rate configured in "
                f"{rig_name or 'the rig file'} (not measured)"
            ),
            "params": params_source or "the params model's defaults",
            "reductions": reductions,
        },
        "simulated": mode is Mode.SIMULATE,
    }

    schedule = task.duration_schedule(params, scratch_rng())
    if isinstance(schedule, UnknownSchedule):
        return {
            **answer,
            "status": "unknown",
            "headline": "No reliable estimate",
            "reason": schedule.reason,
            "manual": [],
            "excluded": [],
            "assumptions": [],
        }

    iti = _iti_s(params, hz)
    timer = _Timer(task, hz)
    manual: list[str] = []
    excluded: list[str] = []
    assumptions: list[str] = []

    if isinstance(schedule, PlannedSchedule):
        conditions = schedule.conditions()
        timings = [timer(c) for c in conditions]
        counts: dict[str, Any] = {
            "kind": "planned",
            "trials": schedule.n_trials,
            "trials_max": schedule.n_trials,
            "blocks": len(schedule.blocks),
            "breaks": schedule.breaks,
            "breaks_max": schedule.breaks,
            "conditions": schedule.n_cells,
        }
        breaks_min = schedule.breaks
        breaks_max: int | None = schedule.breaks
        validate = schedule.validate_after_break
        stopping_rule: str | None = None
    else:
        assert isinstance(schedule, AdaptiveSchedule)
        conditions = list(schedule.conditions)
        timings = [timer(c) for c in conditions]
        counts = {
            "kind": "adaptive",
            "trials": schedule.min_trials,
            "trials_max": schedule.max_trials,
            "blocks": None,
            "breaks": schedule.min_breaks,
            "breaks_max": schedule.max_breaks,
            "conditions": len(conditions),
        }
        breaks_min, breaks_max = schedule.min_breaks, schedule.max_breaks
        validate = schedule.validate_after_break
        stopping_rule = schedule.stopping_rule
    if schedule.note:
        assumptions.append(schedule.note)
    if stopping_rule:
        assumptions.append(f"Stopping rule: {stopping_rule}.")

    # Who presses what, mode by mode. `plus` names, in a few words, what the
    # number leaves out that a person controls.
    tracker = effective.devices.eyetracker
    real_tracker = tracker is not None and tracker.backend not in _STAND_INS
    plus: list[str] = []
    rest_s = 0.0
    if mode is Mode.SIMULATE:
        # Unattended: the rests resume by themselves (included below).
        rest_s = SIMULATION_REST_RESUME_S
        assumptions.append(
            "Simulated session time: a simulated subject on a display paced at the rig's "
            "refresh rate. How long this computer takes to render it is not estimated."
        )
    else:
        manual.append("the instruction screen, if the task shows one (until SPACE)")
        if real_tracker:
            manual.append("eye-tracker calibration before trial 1 (requested at start-up)")
            plus.append("calibration")
        if breaks_max is None or breaks_max > 0:
            n = (
                str(breaks_min)
                if breaks_max == breaks_min
                else f"{breaks_min}–{breaks_max if breaks_max is not None else '…'}"
            )
            word = "break" if breaks_max == breaks_min == 1 else "breaks"
            manual.append(
                f"{n} rest {word} between blocks (each until SPACE"
                + (", then a calibration validation)" if validate else ")")
            )
            plus.append(f"{n} manual {word}")
    if getattr(task, "reward", None) is not None and mode.drives_subject:
        reward = effective.devices.reward
        if reward is not None and reward.backend != "simulated":
            excluded.append("reward deliveries on paid trials (their length depends on outcomes)")

    if any(t is None for t in timings):
        missing = type(task).__name__
        return {
            **answer,
            "status": "partial",
            "counts": counts,
            "headline": (f"{_count_text(counts)} — not timed"),
            "reason": (
                f"{missing} does not declare how long its trials last (Task.trial_timing), "
                "so the trials are counted but not timed"
            ),
            "manual": manual,
            "excluded": excluded,
            "assumptions": assumptions,
        }

    typed = [t for t in timings if t is not None]
    # Two pairs of totals. `low`/`high`, the headline: what the configuration
    # decides at its expected value (a jitter averages out over a session),
    # plus the waits on the subject from their minimum to their cap. `bounds`:
    # every span at its own extreme on every trial, which no session reaches
    # but nothing can exceed.
    if isinstance(schedule, PlannedSchedule):
        low = sum(t.fixed_s + iti for t in typed)
        opens = [t.open_s for t in typed]
        high = None if any(o is None for o in opens) else low + sum(o for o in opens if o)
        bound_min = sum(t.min_s + iti for t in typed)
        maxima = [t.max_s for t in typed]
        bound_max = (
            None
            if any(m is None for m in maxima)
            else sum(m + iti for m in maxima if m is not None)
        )
        attempt_max = max((m for m in maxima if m is not None), default=None)
    else:
        n_min, n_max = schedule.min_trials, schedule.max_trials
        shortest = min(t.fixed_s for t in typed) + iti
        widest = [None if t.open_s is None else t.fixed_s + t.open_s + iti for t in typed]
        longest = None if any(w is None for w in widest) else max(w for w in widest if w)
        low = n_min * shortest
        high = None if n_max is None or longest is None else n_max * longest
        bound_min = n_min * (min(t.min_s for t in typed) + iti)
        maxima = [t.max_s for t in typed]
        bound_max = (
            None
            if n_max is None or any(m is None for m in maxima)
            else n_max * (max(m for m in maxima if m is not None) + iti)
        )
        attempt_max = None if any(m is None for m in maxima) else max(maxima)  # type: ignore[type-var]
        if len(typed) > 1:
            assumptions.append(
                "Adaptive trials are timed over the task's conditions: each as long as the "
                "shortest of them for the low end, the longest for the high end."
            )
    expected = low if high is not None and math.isclose(low, high) else None

    if rest_s and (breaks_max is None or breaks_max > 0):
        low += rest_s * breaks_min
        bound_min += rest_s * breaks_min
        high = None if high is None or breaks_max is None else high + rest_s * breaks_max
        bound_max = (
            None if bound_max is None or breaks_max is None else bound_max + rest_s * breaks_max
        )
        if expected is not None:
            expected = expected + rest_s * breaks_min if breaks_max == breaks_min else None
        assumptions.append(
            f"Each rest between blocks resumes by itself after {rest_s:g} s in simulate mode "
            "(included)."
        )

    waits = sorted(
        {
            f"{s.label} (≤ {s.max_s:g} s)" if s.max_s is not None else s.label
            for t in typed
            for s in t.spans
            if s.kind in ("wait", "range")
        }
    )
    if waits:
        assumptions.append(
            "Jittered durations count at their mean (over a session the draws average out); "
            "waits on the subject and design-bounded stretches count from their minimum to "
            "their cap, never at a typical value: " + "; ".join(waits) + "."
        )
    retry = (
        f"each attempt that does not complete (fixation break, no response, dropped frames) "
        f"is served again and adds up to {duration_text(attempt_max + iti, round_up=True)}"
        if attempt_max is not None
        else "each attempt that does not complete is served again and adds another trial"
    )
    excluded.append(f"re-served trials: {retry}")
    excluded.append(
        "start-up and wrap-up (window, refresh check, devices, saving files) and any pause"
    )

    headline = range_text(low, high, expected)
    return {
        **answer,
        "status": "ok",
        "headline": headline,
        "plus": f"plus {' and '.join(plus)}" if plus else "",
        "seconds": {
            "low": low,
            "high": high,
            "expected": expected,
            "bound_min": bound_min,
            "bound_max": bound_max,
        },
        "counts": counts,
        "per_trial": {
            "iti_s": iti,
            "spans": _span_rows(typed[0], iti) if len(typed) == 1 or _all_same(typed) else None,
            "min_s": min(t.min_s for t in typed) + iti,
            "max_s": None
            if any(t.max_s is None for t in typed)
            else max(t.max_s for t in typed if t.max_s is not None) + iti,
        },
        "manual": manual,
        "excluded": excluded,
        "assumptions": assumptions,
    }


def _all_same(timings: Sequence[TrialTiming]) -> bool:
    first = timings[0].spans
    return all(t.spans == first for t in timings)


def _count_text(counts: Mapping[str, Any]) -> str:
    trials = counts["trials"]
    high = counts.get("trials_max", trials)
    if high == trials:
        text = _plural(trials, "trial")
    elif high is None:
        text = f"at least {trials} trials"
    else:
        text = f"{trials}–{high} trials"
    if counts.get("blocks"):
        text += f" in {_plural(counts['blocks'], 'block')}"
    return text


# ----------------------------------------------------------------------
# The modes that run no trials
# ----------------------------------------------------------------------


def open_ended(mode: Mode) -> dict[str, Any]:
    """Demo: it shows the stimulus until someone stops it."""
    return {
        "schema": SCHEMA,
        "mode": mode.value,
        "status": "open-ended",
        "headline": "Open-ended, until stopped",
        "reason": "Demo mode shows the stimulus until you quit it; it has no end of its own.",
        "manual": [],
        "excluded": [],
        "assumptions": [],
    }


def movie() -> dict[str, Any]:
    """Movie: rendering time is this machine's, not the experiment's."""
    return {
        "schema": SCHEMA,
        "mode": Mode.MOVIE.value,
        "status": "unknown",
        "headline": "No reliable estimate",
        "reason": (
            "Movie mode renders the task's clips to files; how long that takes depends on the "
            "clips and on this computer's rendering and encoding speed, which nothing here "
            "measures."
        ),
        "manual": [],
        "excluded": [],
        "assumptions": [],
    }


def estimate_measure(
    rig: RigConfig,
    jobs: Mapping[str, Any],
    selected: Sequence[str],
    inputs: Mapping[str, str],
) -> dict[str, Any]:
    """Measure rig: the machine-timed sampling of the jobs selected, and the
    operator-guided parts named. ``selected`` empty runs measure mode's
    fixed list, which this does not time."""
    base = {"schema": SCHEMA, "mode": Mode.MEASURE.value, "excluded": [], "assumptions": []}
    if not selected:
        return {
            **base,
            "status": "unknown",
            "headline": "No reliable estimate",
            "reason": "No measurements were selected; the fixed list is not timed.",
            "manual": [],
        }
    timed = 0.0
    rows: list[dict[str, Any]] = []
    manual: list[str] = []
    undeclared: list[str] = []
    for key in selected:
        job = jobs.get(key)
        if job is None:
            raise ValueError(f"{key} is not a measurement this installation offers")
        refusal = job.unavailable(rig, inputs)
        if refusal:
            rows.append(
                {"key": key, "title": job.title, "timed_s": 0.0, "note": f"unavailable: {refusal}"}
            )
            continue
        note = getattr(job, "duration", None)
        if note is None:
            undeclared.append(job.title)
            rows.append({"key": key, "title": job.title, "timed_s": None, "note": "not declared"})
            continue
        seconds = float(note.timed_s(rig, inputs)) if note.timed_s is not None else 0.0
        timed += seconds
        rows.append(
            {
                "key": key,
                "title": job.title,
                "timed_s": seconds,
                "basis": note.timed_basis,
                "operator": note.operator,
            }
        )
        if note.operator:
            manual.append(f"{job.title}: {note.operator}")
    operator_only = timed == 0 and manual
    if undeclared:
        status, headline = (
            "partial",
            (
                f"{duration_text(timed)} timed, plus parts not declared"
                if timed
                else "No reliable estimate"
            ),
        )
    elif operator_only:
        status, headline = "ok", "Operator-dependent"
    else:
        status, headline = "ok", f"≈ {duration_text(timed)} of timed sampling"
    return {
        **base,
        "status": status,
        "headline": headline,
        "plus": "plus operator-guided steps" if manual and not operator_only else "",
        "seconds": {
            "low": timed,
            "high": None if manual or undeclared else timed,
            "expected": None if manual or undeclared else timed,
        },
        "jobs": rows,
        "manual": manual,
        "reason": (f"{', '.join(undeclared)} do not say how long they take" if undeclared else ""),
        "basis": {"refresh_hz": float(rig.monitor.refresh_rate_hz)},
    }
