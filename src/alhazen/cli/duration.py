"""``run.py --estimate-duration``: how long the launch would take, as JSON.

The experiment workspace asks this of the project's own interpreter, with
the same command line it would launch, plus this flag, so the estimate reads
the rig, the task, the params file and the mode exactly as the run would
(``alhazen.cli.main._run_session`` hands over before anything else happens).
It prints one JSON object on its last line of stdout — everything else the
experiment prints while loading goes to stderr — and never opens a window or
a device, never asks for a subject, never runs the params hook and never
writes a file (alhazen.modes.estimate says what is and is not counted).

Exit 0 with ``status`` "ok", "partial", "unknown", "open-ended" or
"refused" (a launch the run itself would refuse, with its words); exit 1
with ``status`` "error" for a rig or params file that does not load. A crash
is a traceback and a non-zero exit, as for any bug.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Any

from pydantic import ValidationError

from alhazen.errors import ConfigError
from alhazen.modes import Mode, flag_refusal, real_data_refusal


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, allow_nan=False))


def estimate_duration(args: argparse.Namespace, task_class: Any = None) -> int:
    """Answer the estimate for the launch ``args`` describes; see the module."""
    mode = Mode(args.mode)
    with contextlib.redirect_stdout(sys.stderr):
        try:
            payload = _estimate(args, task_class, mode)
            code = 0
        except (ConfigError, ValidationError, ValueError) as exc:
            payload = {
                "schema": 1,
                "mode": mode.value,
                "status": "error",
                "headline": "No estimate",
                "reason": str(exc),
            }
            code = 1
    _print(payload)
    return code


def _refused(mode: Mode, reason: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "mode": mode.value,
        "status": "refused",
        "headline": "This launch would be refused",
        "reason": reason,
    }


def _estimate(args: argparse.Namespace, task_class: Any, mode: Mode) -> dict[str, Any]:
    from alhazen.cli.main import (
        _calibration_flags,
        _calibration_images,
        _experiment_root,
        _load_params,
        _real_data_instead,
    )
    from alhazen.config.loader import load_rig
    from alhazen.config.models import with_calibration_target
    from alhazen.config.rigs import resolve_rig
    from alhazen.modes import estimate

    refusal = flag_refusal(
        mode, headless=args.headless, mouse=args.mouse, calibration=_calibration_flags(args)
    )
    if refusal is not None:
        return _refused(mode, refusal)
    if task_class is None and mode is not Mode.MEASURE:
        from alhazen.cli.tasks import load_task_class

        if args.task is None:
            raise ConfigError("the estimate needs --task")
        task_class = load_task_class(args.task)
    if args.rig is None:
        raise ConfigError("the estimate needs --rig")
    root = _experiment_root(task_class)
    ref = resolve_rig(args.rig, root)
    rig = with_calibration_target(
        load_rig(ref.path),
        appearance=args.calibration_target,
        images=_calibration_images(args.calibration_images),
        motion=args.calibration_motion,
    )
    args.rig_ref = ref
    refusal = real_data_refusal(mode, rig, ref, instead=lambda: _real_data_instead(args, root))
    if refusal is not None:
        return _refused(mode, refusal)

    if mode is Mode.MEASURE:
        from alhazen.modes.measure_jobs import installed_jobs, parse_inputs

        return estimate.estimate_measure(
            rig, installed_jobs(), list(args.measure), parse_inputs(args.measure_input)
        )
    if mode is Mode.DEMO:
        return estimate.open_ended(mode)
    if mode is Mode.MOVIE:
        return estimate.movie()
    if getattr(args, "training", None) is not None:
        # A training stage's params: its file, its overrides and its reward
        # (alhazen.cli.main._settle_training_stage resolved it already).
        params, source = args.training.params, args.params
    else:
        params, source = _load_params(task_class, args.params)
    return estimate.estimate_trials(
        mode,
        task_class(params),
        params,
        rig,
        trials_per_condition=args.trials_per_condition,
        headless=args.headless,
        mouse=args.mouse,
        rig_name=ref.name,
        params_source=source,
    )
