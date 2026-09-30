"""What a real eye and a real tracker add to where a subject means to look.

A simulated subject decides where it is looking — the fixation point, then
the target — and every one of alhazen's experiments wrote that decision
straight into its gaze samples. So a simulated eye held perfectly still: the
same pixel on every frame of a fixation, a flat line in the live monitor's
gaze trace, a fixation window whose edge no sample ever came near. No real
subject does that, and a rehearsal whose eye never moves cannot show whether
a window is too tight, whether a stability check ever settles, or whether a
landing measurement copes with an eye that is still moving a little.

:class:`GazeNoise` is the part of a real gaze signal that has nothing to do
with the task. It is added to the point the subject means to look at, and it
has three parts, with default sizes taken from the ranges reported for
fixational eye movements (reviewed by Martinez-Conde, Macknik & Hubel 2004)
and for video trackers' own noise:

- **drift**: the eye wanders slowly away from the point it is fixating and
  is pulled back. Modelled as a mean-reverting random walk (an
  Ornstein-Uhlenbeck process) with a standard deviation ``drift_sd_dva`` per
  axis and a time constant ``drift_tau_s``;
- **microsaccades**: small quick jumps, ``microsaccade_rate_hz`` of them a
  second, of a typical size ``microsaccade_dva``, aimed roughly back at the
  point being fixated (which is what microsaccades mostly do);
- **tracker noise**: what the camera adds to every sample on its own, white,
  with a standard deviation ``tracker_sd_dva`` per axis.

The offset keeps running through saccades: the eye lands on a new point and
goes on wandering around it. That is what a subject does, and it is why the
caller adds the offset to whatever point its own behaviour says the eye is
aimed at, on every sample, rather than only during fixations.

It works in degrees and knows nothing about pixels or screens, because the
simulated subjects that use it keep their positions in different pixel
frames (screen px with y down, centred px with y up). The offset is the same
size in every direction, so which way y grows does not matter; the caller
multiplies by its screen's pixels per degree.

Frame-rate independent: each step is exact for the time that passed since
the last sample, however long, so the scatter is the same at 60 Hz and at
240 Hz, and a slow frame does not make the eye jump.
"""

from __future__ import annotations

import math

import numpy as np

# How widely a microsaccade's size spreads around its typical size: the
# standard deviation of its logarithm. 0.4 puts nearly all of them between
# about a third of the typical size and three times it, which is the spread
# the literature reports. A constant rather than a setting: nobody tuning a
# rehearsal needs it, and one fewer knob is one fewer thing to record.
MICROSACCADE_LOG_SD = 0.4

# How far a microsaccade's direction strays from pointing straight back at
# the fixated point, as a standard deviation in degrees of angle. They are
# corrective on average, not perfectly aimed.
MICROSACCADE_AIM_SD_DEG = 45.0

# After this many time constants the wander has forgotten where it was
# (what is left of the old offset is e**-10, under a hundred-thousandth), so
# the new offset is drawn afresh instead of stepping through every
# microsaccade of a long gap one at a time. A long gap is routine: the time
# between two trials, or a break between blocks.
FORGET_AFTER_TAUS = 10.0


class GazeNoise:
    """The eye's own movement and the tracker's noise, as an offset in degrees.

    Ask it for the offset at each sample's time, and add the offset to the
    point the simulated subject is aiming at::

        noise = GazeNoise(np.random.default_rng(seed))
        dx, dy = noise.offset_dva(clock.now())
        gaze_px = (aim_px[0] + dx * px_per_deg, aim_px[1] + dy * px_per_deg)

    Give it **its own generator**, not the one the subject draws its trials
    from. How many numbers it draws depends on how many samples are taken,
    and that depends on the frame timing; a generator shared with the trial
    plan would make trial 20's plan depend on how fast trial 19 was drawn,
    and a seeded rehearsal would stop replaying.

    Setting every size to zero gives an offset of exactly zero, which is a
    still eye: what a test that checks the subject's behaviour, rather than
    its noise, can ask for.
    """

    def __init__(
        self,
        rng: np.random.Generator,
        *,
        tracker_sd_dva: float = 0.02,
        drift_sd_dva: float = 0.15,
        drift_tau_s: float = 0.5,
        microsaccade_rate_hz: float = 1.0,
        microsaccade_dva: float = 0.3,
    ) -> None:
        # Refused here, by name, rather than producing NaN positions a whole
        # rehearsal later: a negative standard deviation makes numpy raise
        # mid-session, and a zero time constant divides by zero.
        for name, value in (
            ("tracker_sd_dva", tracker_sd_dva),
            ("drift_sd_dva", drift_sd_dva),
            ("microsaccade_rate_hz", microsaccade_rate_hz),
            ("microsaccade_dva", microsaccade_dva),
        ):
            if not (math.isfinite(value) and value >= 0.0):
                raise ValueError(f"GazeNoise: {name} must be a finite number >= 0, got {value!r}")
        if not (math.isfinite(drift_tau_s) and drift_tau_s > 0.0):
            raise ValueError(
                f"GazeNoise: drift_tau_s must be a finite number > 0, got {drift_tau_s!r}"
            )

        self._rng = rng
        self._tracker_sd = float(tracker_sd_dva)
        self._drift_sd = float(drift_sd_dva)
        self._tau = float(drift_tau_s)
        self._microsaccade_rate = float(microsaccade_rate_hz)
        self._microsaccade_dva = float(microsaccade_dva)

        # Where the eye is, relative to the point it means to look at, in
        # degrees: the drift and every microsaccade so far. None until the
        # first sample, when it is drawn from the wander's own spread — the
        # eye was already wandering before anybody looked.
        self._eye: np.ndarray | None = None
        # The last sample handed out, and its time. Asked twice for the same
        # instant (two readers in one frame), it answers the same: a tracker
        # has one newest sample, not a fresh one per question.
        self._last_t: float | None = None
        self._last_offset = (0.0, 0.0)

    def offset_dva(self, t: float) -> tuple[float, float]:
        """The gaze offset at time ``t`` (seconds, the session clock), in degrees.

        ``t`` must not go backwards: the offset is a trajectory, and a sample
        from the past cannot be drawn after the future has been. A clock
        that runs backwards is a bug in the caller, so it is refused loudly
        rather than answered with something plausible.
        """
        if self._last_t is not None:
            if t < self._last_t:
                raise ValueError(
                    f"GazeNoise.offset_dva: time went backwards, from {self._last_t!r} s "
                    f"to {t!r} s; the offset is a trajectory and can only move forward"
                )
            if t == self._last_t:
                return self._last_offset

        if self._eye is None:
            self._eye = self._stationary()
        else:
            assert self._last_t is not None
            self._advance(t - self._last_t)

        # The tracker's noise is on the measurement, not on the eye: it is
        # drawn fresh for every sample and never carried into the next one.
        measured = self._eye + self._rng.normal(0.0, self._tracker_sd, size=2)
        self._last_t = t
        self._last_offset = (float(measured[0]), float(measured[1]))
        return self._last_offset

    def describe(self) -> dict[str, float]:
        """The settings, for the run's snapshot (``Simulation.describe``)."""
        return {
            "tracker_sd_dva": self._tracker_sd,
            "drift_sd_dva": self._drift_sd,
            "drift_tau_s": self._tau,
            "microsaccade_rate_hz": self._microsaccade_rate,
            "microsaccade_dva": self._microsaccade_dva,
        }

    # ------------------------------------------------------------------
    # The eye's own movement
    # ------------------------------------------------------------------

    def _stationary(self) -> np.ndarray:
        """An offset drawn from the wander's long-run spread."""
        return self._rng.normal(0.0, self._drift_sd, size=2)

    def _advance(self, dt: float) -> None:
        """Move the eye on by ``dt`` seconds: drift, with microsaccades in it."""
        if dt > FORGET_AFTER_TAUS * self._tau:
            self._eye = self._stationary()
            return
        # The microsaccades in this interval arrive at random (a Poisson
        # process), each at its own moment inside it; the drift runs between
        # them. Stepping from one to the next keeps the drift exact whatever
        # the frame rate.
        count = int(self._rng.poisson(self._microsaccade_rate * dt))
        moments = np.sort(self._rng.uniform(0.0, dt, size=count))
        elapsed = 0.0
        for moment in moments:
            self._drift(float(moment) - elapsed)
            self._microsaccade()
            elapsed = float(moment)
        self._drift(dt - elapsed)

    def _drift(self, dt: float) -> None:
        """The exact Ornstein-Uhlenbeck step over ``dt``.

        The offset decays toward zero by ``exp(-dt / tau)`` and gains just
        enough fresh randomness that its spread stays ``drift_sd_dva``
        however it is chopped into steps: the variance added is
        ``sd² (1 - decay²)``.
        """
        assert self._eye is not None
        decay = math.exp(-dt / self._tau)
        spread = self._drift_sd * math.sqrt(1.0 - decay * decay)
        self._eye = self._eye * decay + self._rng.normal(0.0, spread, size=2)

    def _microsaccade(self) -> None:
        """One small jump, aimed roughly back at the fixated point."""
        assert self._eye is not None
        if self._microsaccade_dva == 0.0:
            return
        size = float(self._rng.lognormal(math.log(self._microsaccade_dva), MICROSACCADE_LOG_SD))
        # Straight back toward the fixated point, then strayed from it. An eye
        # sitting exactly on the point has no "back", so any direction will do.
        if float(np.hypot(*self._eye)) > 0.0:
            home = math.atan2(-self._eye[1], -self._eye[0])
        else:
            home = float(self._rng.uniform(-math.pi, math.pi))
        angle = home + math.radians(float(self._rng.normal(0.0, MICROSACCADE_AIM_SD_DEG)))
        self._eye = self._eye + size * np.array([math.cos(angle), math.sin(angle)])
