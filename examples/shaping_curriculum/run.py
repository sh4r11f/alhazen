"""Shape a scripted subject through three stages, twice.

    python examples/shaping_curriculum/run.py --data-root ~/tmp/shaping

Run it once and the subject works its way up the curriculum. Run it again
against the same data root and it carries on where it left off — the point of
the demonstration, since shaping happens over weeks and a session is a day.

The "subject" here is a scripted tracker that gets better with practice: it
starts by looking roughly at the point and gradually looks more precisely, so
the criteria promote it. It is a demonstration of the machinery, not a model
of an animal.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from task import ShapingParams, ShapingTask

from alhazen import Screen, build_session
from alhazen.config.loader import load_model, load_rig
from alhazen.core.clock import Clock, MonotonicClock
from alhazen.devices.eyetracker import GazeSample, HostShape
from alhazen.training import Curriculum

HERE = Path(__file__).parent


class ImprovingSubject:
    """A subject whose fixation tightens as it does more trials.

    Gaze starts scattered a few degrees around the point and converges on it
    over the session, which is what lets the curriculum's criteria fire in a
    demonstration that takes seconds rather than weeks.

    A complete eye tracker as far as the session is concerned, so it is
    handed over as one — ``build_session(tracker=ImprovingSubject(), ...)``
    — and the session wires it like any other: ``configure`` gives it the
    screen and the session clock, the engine reads its gaze, and the
    session's tracker messages tell it each time a trial starts.
    """

    def __init__(self, start_error_dva: float = 5.0) -> None:
        self._start_error_dva = start_error_dva
        # Set by configure(): the session's geometry and its one clock.
        self._screen: Screen | None = None
        self._clock: Clock | None = None
        self._trials = 0
        self._recording = False

    # -- the part that makes it "improve" ------------------------------

    def _error_px(self, screen: Screen) -> float:
        # Halves every twenty trials: fast enough to walk a demonstration
        # through three stages, slow enough that each stage sees several
        # trials at its own difficulty.
        return screen.deg2px(self._start_error_dva) * (0.5 ** (self._trials / 20.0))

    # -- the EyeTracker protocol ---------------------------------------

    def connect(self) -> None:
        return

    def configure(self, screen: Screen, clock: Clock) -> None:
        # Called by build_session with the session's own screen and clock,
        # so the gaze is placed in this session's geometry and stamped on
        # the same timebase as everything else the session records.
        self._screen = screen
        self._clock = clock

    def calibrate(self) -> None:
        return

    def start_trial(self, trial_index: int, status: str) -> None:
        self._recording = True

    def stop_trial(self) -> None:
        self._recording = False

    def is_recording(self) -> bool:
        return self._recording

    def get_gaze(self) -> GazeSample | None:
        if self._screen is None or self._clock is None:
            raise RuntimeError(
                "ImprovingSubject.get_gaze() before configure(): hand it to "
                "build_session(tracker=...), which configures it with the session's "
                "screen and clock"
            )
        # Offset along one axis only, so the demonstration is deterministic:
        # the improvement is the point, not the noise.
        gx, gy = self._screen.centered_to_screen(self._error_px(self._screen), 0.0)
        return GazeSample(gx=gx, gy=gy, t=self._clock.now())

    def send_message(self, text: str) -> None:
        # The session writes one tracker message per event it emits (what a
        # real tracker stores in its recording for alignment), and with no
        # message map of its own a TRIAL_START arrives as "trial_start".
        # Counting those is how the subject knows how much practice it has
        # had: every attempt, once, as the session sees it.
        if text == "trial_start":
            self._trials += 1

    def draw_host_overlay(self, shapes: list[HostShape]) -> None:
        return

    def shutdown(self, edf_destination: Path | None) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rig", default=None, help="rig YAML (in this directory)")
    parser.add_argument("--data-root", default=None, help="override the rig's data_root")
    parser.add_argument("--trials", type=int, default=60, help="trials to run this session")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--auto", action="store_true", help="run the demo in a real window")
    args = parser.parse_args()

    rig_path = HERE / (args.rig or ("rig-psychopy.yaml" if args.auto else "rig-sim.yaml"))
    rig = load_rig(rig_path)
    if args.data_root is not None:
        rig = rig.model_copy(update={"data_root": Path(args.data_root)})
    params = load_model(HERE / "task.yaml", ShapingParams)
    params = params.model_copy(
        update={"paradigm": params.paradigm.model_copy(update={"n_per_condition": args.trials})}
    )
    curriculum = load_model(HERE / "curriculum.yaml", Curriculum)

    # The session clock, made here and handed to build_session, which passes
    # the same one to the stand-in subject (configure) so its gaze is stamped
    # on the timebase of everything else recorded. A real one: with --auto
    # this runs in a real window.
    clock = MonotonicClock()
    runner = build_session(
        rig=rig,
        subject="m01",
        session=1,
        run=_next_run(rig.data_root),
        task=ShapingTask(params),
        curriculum=curriculum,
        seed=args.seed,
        iti=params.iti,
        # The stand-in subject, where a real rig's tracker goes. A real rig
        # names its tracker in the rig config instead.
        tracker=ImprovingSubject(),
        simulated_frame_period_s=0.0,
        windowed=True,
        sources={"rig": str(rig_path), "task": str(HERE / "task.yaml")},
        instructions=(HERE / "instructions.md").read_text(),
        auto_start=args.auto,
        clock=clock,
    )
    runner.run()
    state_path = rig.data_root / "sub-m01" / "training_state.yaml"
    print(f"session complete — training state at {state_path}")


def _next_run(data_root: Path) -> int:
    session_dir = data_root / "sub-m01" / "ses-001"
    if not session_dir.exists():
        return 1
    taken = [int(p.name.split("_")[0].split("-")[1]) for p in session_dir.glob("run-*")]
    return max(taken, default=0) + 1


if __name__ == "__main__":
    main()
