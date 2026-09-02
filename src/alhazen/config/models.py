"""Validated configuration models.

Every model forbids unrecognized keys (``extra="forbid"``) so a typo in a
YAML file fails loudly at load time instead of silently falling back to a
default. Units are part of field names (``_px``, ``_cm``, ``_ms``, ``_hz``,
``_dva``) so a bare ``size`` or ``timeout`` is never ambiguous.

Durations are frames-first citizens: anywhere a duration is configured, it
may be given in milliseconds *or* in display frames (`Duration`), and is
resolved once against the measured refresh rate — never ad-hoc ``ms2fr``
arithmetic inside task code.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from alhazen.errors import ConfigError


class Model(BaseModel):
    """Base for all config models: unknown keys are errors, values frozen
    after validation (a session's config must not drift mid-run — runtime
    parameter changes are events, not config mutations)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Duration(Model):
    """A duration expressed in exactly one of milliseconds or display frames.

    ``frames`` is exact by construction; ``ms`` is resolved to the nearest
    whole frame when frame counts are needed. Both resolutions require the
    display's *measured* refresh rate, so they happen at session build time,
    not at config load time.
    """

    ms: float | None = None
    frames: int | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Duration:
        if (self.ms is None) == (self.frames is None):
            raise ValueError("give exactly one of 'ms' or 'frames'")
        if self.ms is not None and self.ms < 0:
            raise ValueError("ms must be >= 0")
        if self.frames is not None and self.frames < 0:
            raise ValueError("frames must be >= 0")
        return self

    def seconds(self, refresh_rate_hz: float) -> float:
        if self.ms is not None:
            return self.ms / 1000.0
        assert self.frames is not None
        return self.frames / refresh_rate_hz

    def n_frames(self, refresh_rate_hz: float) -> int:
        """Whole display frames. A millisecond duration rounds to nearest —
        the rounding is done once, here, so every consumer of the same config
        agrees on the same frame count."""
        if self.frames is not None:
            return self.frames
        assert self.ms is not None
        return round(self.ms / 1000.0 * refresh_rate_hz)


class MonitorConfig(Model):
    """One physical display: pixel grid, physical size, viewing distance."""

    width_px: int
    height_px: int
    width_cm: float
    distance_cm: float
    refresh_rate_hz: float
    screen_index: int = 0
    fullscreen: bool = True
    # The name this panel is registered under in PsychoPy's per-machine
    # monitor database (`alhazen monitor register`), which is also the name
    # Monitor Center and any other PsychoPy script on this machine look it up
    # by. A machine that drives more than one panel must give each rig config
    # its own name: two rigs left on the default would share one registration
    # and overwrite each other's geometry.
    name: str = "alhazen"

    @model_validator(mode="after")
    def _positive(self) -> MonitorConfig:
        for field in ("width_px", "height_px", "width_cm", "distance_cm", "refresh_rate_hz"):
            if getattr(self, field) <= 0:
                raise ValueError(f"{field} must be > 0")
        # The name becomes a file inside PsychoPy's monitor folder, so a path
        # separator in it would write somewhere else entirely, and surrounding
        # whitespace makes two names that look identical in a config file
        # resolve to two different monitors.
        if not self.name or self.name != self.name.strip():
            raise ValueError("monitor name must be non-empty and free of surrounding whitespace")
        if any(bad in self.name for bad in ("/", "\\")) or self.name in (".", ".."):
            raise ValueError(f"monitor name must be a plain name, not a path: {self.name!r}")
        return self


class FrameQAConfig(Model):
    """What to do about dropped frames.

    A frame is "dropped" when its measured interval exceeds the nominal frame
    period by more than ``tolerance`` frame periods (0.5 = halfway to the
    next vsync). Policies escalate: ``log`` (debug log only), ``warn``
    (session log warning), ``mark_trial`` (also count on the trial record so
    analysis can exclude), ``abort_run`` (also raise FrameQAError once
    ``max_dropped_per_trial`` is exceeded in one trial).
    """

    policy: Literal["log", "warn", "mark_trial", "abort_run"] = "warn"
    tolerance: float = 0.5
    max_dropped_per_trial: int = 3

    @model_validator(mode="after")
    def _valid(self) -> FrameQAConfig:
        if not 0.0 < self.tolerance < 1.0:
            raise ValueError("tolerance must be in (0, 1) — fractions of one frame period")
        if self.max_dropped_per_trial < 0:
            raise ValueError("max_dropped_per_trial must be >= 0")
        return self


class PhotodiodeConfig(Model):
    """A luminance patch in one screen corner, flipped white on exactly the
    frames whose flip carries one of ``events``.

    That is the same flip the event's timestamp and its sync pulse mark, so a
    photodiode taped over the patch and a TTL line recorded on the same
    device measure the same instant — which is what makes the software
    timestamp auditable after the fact. ``events`` are the experiment's own
    declared event names, validated against its EventSchema at build time.
    """

    corner: Literal["tl", "tr", "bl", "br"] = "br"
    size_px: int = 60
    events: list[str] = []

    @model_validator(mode="after")
    def _valid(self) -> PhotodiodeConfig:
        if self.size_px <= 0:
            raise ValueError("size_px must be > 0")
        return self


class DisplayConfig(Model):
    backend: Literal["psychopy", "simulated"] = "psychopy"
    frame_qa: FrameQAConfig = FrameQAConfig()
    # None = no patch drawn at all, which is the default. A rig with a
    # photodiode attached names the corner it is taped over and the events it
    # should mark.
    photodiode: PhotodiodeConfig | None = None
    # Warm-up flips before trial 1, used to measure the actual refresh rate.
    # The measured value is what frame math uses; a mismatch beyond
    # refresh_tolerance_hz versus the monitor's nominal rate is a loud error
    # (a 240 Hz panel silently running at 60 Hz invalidates every duration).
    warmup_flips: int = 30
    refresh_tolerance_hz: float = 5.0


class DashboardConfig(Model):
    """The local, between-trial browser dashboard.

    It is opt-in so existing rig files and unattended sessions never start a
    server unexpectedly.  Port zero asks the OS for an unused local port.
    """

    enabled: bool = False
    auto_open: bool = True
    port: int = 0
    # How many of the most recent trials and events each between-trial update
    # carries. Every update serialises what it sends, so sending the whole
    # history each time makes a session's publishing cost grow with the square
    # of its length — a 2000-trial session spends real time on it between
    # trials. Totals travel alongside, and the state saved at teardown is
    # always complete.
    max_rows: int = 1000

    @model_validator(mode="after")
    def _valid(self) -> DashboardConfig:
        if self.port != 0 and not 1024 <= self.port <= 65535:
            raise ValueError("dashboard port must be 0 or between 1024 and 65535")
        if self.max_rows < 1:
            raise ValueError("max_rows must be >= 1")
        return self


class DatabaseConfig(Model):
    """The queryable SQLite mirror of this rig's runs.

    ``artifact_max_bytes`` bounds what is copied *into* the database. Every
    file a run produced is recorded with its path, size and sha256 whatever
    its size — that is what makes it identifiable later — but a file over the
    cap keeps its bytes only where they already are, in the run directory,
    with a log line saying so. Without a cap an EyeLink EDF silently doubles
    a session's footprint, and a season of recording turns into a database
    nobody can move.
    """

    enabled: bool = True
    artifact_max_bytes: int = 10_000_000

    @model_validator(mode="after")
    def _valid(self) -> DatabaseConfig:
        if self.artifact_max_bytes < 0:
            raise ValueError("artifact_max_bytes must be >= 0 (0 stores no file contents)")
        return self


# Which fields on EyeTrackerConfig belong to which backend. A field set on
# the wrong backend is a config error, not a harmless extra: the experimenter
# who wrote it believes they configured something, and nothing at runtime
# would ever tell them otherwise. Fields absent from both maps are shared.
EYELINK_ONLY_FIELDS = ("host_ip", "edf_host_filename")
VIEWPIXX_ONLY_FIELDS = ("eye", "led_intensity", "camera_image")

# Target layouts alhazen can lay out itself, for backends whose calibration
# it drives (viewpixx). The EyeLink accepts more of them, but its Host PC
# owns the grid, so alhazen never has to enumerate the positions.
SELF_DRIVEN_CALIBRATION_TYPES = ("HV5", "HV9", "HV13")
# The layouts the EyeLink Host PC accepts for its ``calibration_type``
# command. Checked at load time: the Host PC would otherwise report the bad
# name on its own screen, in another room, with the subject already seated.
EYELINK_CALIBRATION_TYPES = ("H3", "HV3", "HV5", "HV9", "HV13")


class EyeTrackerConfig(Model):
    """Which eye tracker this rig has, and how it is set up.

    Four backends, selected by ``backend``:

    - ``eyelink`` drives a real SR Research EyeLink over the tracker subnet,
      via a Host PC that writes its own EDF;
    - ``viewpixx`` drives a VPixx TRACKPixx3 inside the display chassis,
      which has no Host PC and streams into the DATAPixx3's own buffer;
    - ``mouse_sim`` fakes gaze with the mouse cursor on a machine with no
      tracker;
    - ``scripted`` is a deterministic replay double that only a test can
      supply a trajectory to — session build and ``check-rig`` both reject it
      (devices/eyetracker/__init__.py).

    Most fields belong to exactly one backend, and setting one that the
    chosen backend ignores is rejected below rather than quietly dropped.
    The shared ones describe the calibration procedure itself, which every
    backend runs the same way from the experimenter's side: a target grid of
    ``calibration_type`` layout covering ``calibration_area`` of the screen,
    walked by hand or automatically (``calibration_advance``), then checked
    (``validate_after_calibration``) against ``accuracy_max_deg``.
    """

    backend: Literal["eyelink", "viewpixx", "mouse_sim", "scripted"]
    host_ip: str = "100.1.1.1"  # EyeLink Host PC, on the isolated tracker subnet
    calibration_type: str = "HV5"  # target layout (5-point horizontal/vertical)
    calibration_area: float = 0.6  # fraction of the screen the target grid spans
    # How the calibration moves from one target to the next. "manual": the
    # experimenter watches the subject and presses SPACE to accept each
    # target — the safe default, because a target accepted while the subject
    # looked elsewhere fits the gaze model to the wrong point and every
    # sample in the session inherits the error. "auto": the tracker accepts a
    # target by itself once gaze has settled on it (the EyeLink's own
    # automatic calibration; alhazen's walk for the TRACKPixx3), for a
    # subject who cannot be waited on — an animal that will not hold a
    # fixation long enough for a hand on the keyboard to catch it.
    calibration_advance: Literal["manual", "auto"] = "manual"
    # Run a validation right after every calibration that took — not one the
    # experimenter aborted, nor one the tracker itself called bad, since there
    # is nothing to measure against then: the same targets shown again, gaze
    # measured against them, and the errors reported on the dashboard. Off
    # only for a rig that validates some other way.
    validate_after_calibration: bool = True
    # A validation passes when its WORST target error is at most this many
    # degrees of visual angle. The worst, not the mean: one corner the model
    # gets wrong is one region of the screen the whole session gets wrong.
    accuracy_max_deg: float = 1.0
    # A drift correction is refused when the measured offset exceeds this.
    # A drift is a small shift from a headrest settling or a camera nudge; an
    # offset of several degrees is a calibration that no longer applies, and
    # shifting the whole gaze model by it would only hide that.
    drift_max_deg: float = 3.0
    edf_host_filename: str = "alhazen.EDF"  # what the EyeLink Host PC saves its recording as
    # TRACKPixx3 is always binocular and a GazeSample carries one position, so
    # which eye that is has to be stated rather than guessed. "average" needs
    # both eyes tracked (devices/eyetracker/viewpixx.py explains why).
    eye: Literal["left", "right", "average"] = "left"
    # TRACKPixx3 IR illuminator, 1-8. None leaves whatever VPixx's own tools
    # set on the device — the same division of labour as the EyeLink, whose
    # camera setup lives on its Host PC and not in this file.
    led_intensity: int | None = None
    # Show the TRACKPixx3's camera image on the dashboard's eye-tracker
    # panel, read while the session is paused or calibrating. The EyeLink's
    # camera lives on its Host PC, which has its own screen for it.
    camera_image: bool = True

    @model_validator(mode="after")
    def _valid(self) -> EyeTrackerConfig:
        if not 0.0 < self.calibration_area <= 1.0:
            raise ValueError("calibration_area must be in (0, 1] — a fraction of the screen")
        if self.accuracy_max_deg <= 0.0:
            raise ValueError("accuracy_max_deg must be > 0 (degrees of visual angle)")
        if self.drift_max_deg <= 0.0:
            raise ValueError("drift_max_deg must be > 0 (degrees of visual angle)")
        self._reject_other_backends_fields()
        if self.backend == "eyelink":
            self._valid_edf_filename()
            self._valid_eyelink_layout()
        if self.backend == "viewpixx":
            self._valid_viewpixx()
        return self

    def _valid_eyelink_layout(self) -> None:
        if self.calibration_type not in EYELINK_CALIBRATION_TYPES:
            raise ValueError(
                f"calibration_type {self.calibration_type!r} is not one the EyeLink accepts — "
                f"use one of {', '.join(EYELINK_CALIBRATION_TYPES)}"
            )

    def _reject_other_backends_fields(self) -> None:
        """Refuse a field that the chosen backend has no use for.

        ``model_fields_set`` is what makes this possible: it holds only the
        keys the YAML actually supplied, so a default that happens not to
        apply (every mouse_sim rig carries the EyeLink's default host_ip)
        stays silent, while a value someone typed on purpose does not.
        """
        wrong_for: dict[str, tuple[str, ...]] = {
            "eyelink": VIEWPIXX_ONLY_FIELDS,
            "viewpixx": EYELINK_ONLY_FIELDS,
        }
        # The simulated backends have no hardware fields at all, so every
        # backend-specific key is wrong on them.
        wrong = wrong_for.get(self.backend, EYELINK_ONLY_FIELDS + VIEWPIXX_ONLY_FIELDS)
        named = sorted(set(wrong) & self.model_fields_set)
        if named:
            raise ValueError(
                f"eyetracker backend {self.backend!r} ignores {', '.join(named)} — "
                f"remove {'them' if len(named) > 1 else 'it'}, or change the backend"
            )

    def _valid_edf_filename(self) -> None:
        stem, dot, ext = self.edf_host_filename.rpartition(".")
        # The EyeLink Host PC writes to an 8.3 filesystem: a longer or
        # non-alphanumeric name is silently rejected by the Host at
        # openDataFile() time — i.e. after the subject is already in the rig.
        if not dot or ext.upper() != "EDF" or not stem.isalnum() or len(stem) > 8:
            raise ValueError(
                f"edf_host_filename {self.edf_host_filename!r} must be 8.3 format for the "
                f"EyeLink Host PC: at most 8 alphanumeric characters plus '.EDF'"
            )

    def _valid_viewpixx(self) -> None:
        # The TRACKPixx3 has no Host PC to own a target grid, so alhazen lays
        # one out itself and can only honour the layouts it can construct.
        # Checked here, at load time, not when the experimenter presses the
        # calibrate key with a subject already in the chair.
        if self.calibration_type not in SELF_DRIVEN_CALIBRATION_TYPES:
            raise ValueError(
                f"calibration_type {self.calibration_type!r} is not one alhazen can lay out "
                f"for the viewpixx backend — use one of "
                f"{', '.join(SELF_DRIVEN_CALIBRATION_TYPES)}"
            )
        if self.led_intensity is not None and not 1 <= self.led_intensity <= 8:
            raise ValueError("led_intensity must be in 1-8 (the TRACKPixx3 illuminator range)")


class RewardHwConfig(Model):
    """The reward line: ``nidaq`` drives a real pump/solenoid through an
    NI-DAQ analog output; ``simulated`` records deliveries instead."""

    backend: Literal["nidaq", "simulated"]
    device: str = "Dev1"  # NI-DAQ device name as enumerated by NI-MAX
    channel: str = "ao0"  # analog-output channel the pump driver listens on
    voltage: float = 5.0


class SyncHwConfig(Model):
    """TTL sync outputs: ``nidaq`` pulses real digital lines for an external
    recording system, ``simulated`` records them, ``none`` disables sync.

    ``event_lines`` maps the *experiment's own* event names to physical
    lines, e.g. ``{"STIM_ON": "Dev1/port0/line0"}``. It is the single source
    of truth for which events reach hardware: an event with no entry pulses
    nothing, by design. Keys are validated against the experiment's
    EventSchema when the session is built (session/builder.py).
    """

    backend: Literal["nidaq", "simulated", "none"]
    pulse_ms: float = 2.0
    event_lines: dict[str, str] = {}

    @model_validator(mode="after")
    def _valid(self) -> SyncHwConfig:
        if self.pulse_ms <= 0:
            raise ValueError("pulse_ms must be > 0")
        return self


class RewardPulses(Model):
    """One reward delivery: a train of ``n_pulses`` pulses of ``pulse_ms``
    separated by ``inter_pulse_ms``. Pulse width is what sets the volume
    delivered, so it is configuration, never a hard-coded constant."""

    n_pulses: int = 2
    pulse_ms: int = 200
    inter_pulse_ms: int = 200

    @model_validator(mode="after")
    def _valid(self) -> RewardPulses:
        for name in ("n_pulses", "pulse_ms", "inter_pulse_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        return self


class RecordingConfig(Model):
    """The neural/physiological recorder running alongside this session.

    alhazen never records that data; it records where to find it. ``data_dir``
    is where the acquisition software writes, and ``run_glob`` the naming
    convention this rig uses, so a run directory can point at its own
    external recording (devices/recording.py).
    """

    backend: Literal["spikeglx", "simulated"]
    data_dir: Path = Path(".")
    run_glob: str = "*_g0"

    @model_validator(mode="after")
    def _valid(self) -> RecordingConfig:
        if not self.run_glob:
            raise ValueError("run_glob must name the recording run's directory pattern")
        return self


# Which fields on SpikeSourceConfig each backend actually reads, enforced
# the same way EyeTrackerConfig enforces its split: a field set on a backend
# that never reads it is a config error, because the experimenter who wrote
# it believes they configured something and nothing at runtime would say
# otherwise.
#
# Stated as "what this backend uses" rather than "what only this backend
# uses" because one field is genuinely shared: fetch_interval_ms paces the
# background poll loop, and two of the three backends run one.
SPIKEGLX_FIELDS = (
    "host",
    "port",
    "stream",
    "channels",
    "fetch_interval_ms",
    "threshold_sigmas",
    "hp_window_ms",
    "refractory_ms",
    "car",
)
SIMULATED_SPIKES_FIELDS = (
    "sim_channels",
    "sim_rf_centers_dva",
    "sim_rf_sigma_dva",
    "sim_baseline_hz",
    "sim_peak_hz",
    "sim_latency_ms",
    "sim_duration_ms",
    "sim_respond_to",
    "sim_seed",
)
SORTED_STREAM_FIELDS = (
    "address",
    "fetch_interval_ms",
    "heartbeat_timeout_ms",
)
BACKEND_FIELDS = {
    "spikeglx": SPIKEGLX_FIELDS,
    "simulated": SIMULATED_SPIKES_FIELDS,
    "sorted_stream": SORTED_STREAM_FIELDS,
}

# A ZeroMQ endpoint: a transport, then an address. Validated at load time so
# a bare "host:port" — which zmq.connect() accepts and then never receives
# anything on — fails with the file open.
_ZMQ_ADDRESS_RE = r"^(tcp|ipc|inproc|pgm|epgm)://.+$"

# "all", or comma-separated entries of "N" / "N:M" (inclusive), e.g.
# "0:383", "0:127,256:383", "5,9,12". Validated at load time so a malformed
# list fails with the file open, not mid-session.
_CHANNELS_RE = r"^(all|\d+(:\d+)?(,\d+(:\d+)?)*)$"


class SpikeSourceConfig(Model):
    """A live spike stream read *during* the session, for online analysis.

    Distinct from ``RecordingConfig`` on purpose: that one records where the
    acquisition host's files land so a run can be found and aligned
    afterwards; this one opens a network connection to the running
    acquisition and streams samples back live. A rig doing chronic
    recordings typically configures both.

    Three backends:

    - ``spikeglx`` connects to SpikeGLX's remote command server (Options →
      Command Server in SpikeGLX; default port 4142) through the official
      SpikeGLX-CPP-SDK Python bindings, fetches the stream in the
      background, and turns it into threshold-crossing spikes
      (:mod:`alhazen.neural.detect`);
    - ``sorted_stream`` subscribes to an external real-time sorter
      publishing already-sorted units over ZeroMQ, so the rows a live
      analysis sees are units rather than channels (see
      ``docs/live-spikes.md`` for the wire contract);
    - ``simulated`` invents spikes from configured ground-truth receptive
      fields, driven by the session's own stimulus events — which is what
      lets the whole live pipeline run, and be tested, with no probe in any
      brain.
    """

    backend: Literal["spikeglx", "simulated", "sorted_stream"]

    # --- spikeglx ------------------------------------------------------
    host: str = "127.0.0.1"  # the machine SpikeGLX runs on
    port: int = 4142  # SpikeGLX's command-server port
    # Which stream to read: "imec0", "imec1", ... for Neuropixels probes,
    # "nidq" for the NI-DAQ stream, "obx0", ... for a OneBox.
    stream: str = "imec0"
    # Which channels to monitor: "all" (every AP channel of an imec stream),
    # or an explicit list of entries "N" / "N:M" (inclusive), e.g. "0:383".
    channels: str = "all"
    fetch_interval_ms: float = 200.0
    # Detection: threshold at -threshold_sigmas x the robust noise estimate,
    # after a moving-average high-pass and (optionally) a common-average
    # reference across channels (alhazen.neural.detect).
    threshold_sigmas: float = 4.5
    hp_window_ms: float = 5.0
    refractory_ms: float = 1.0
    car: bool = True

    # --- simulated -----------------------------------------------------
    sim_channels: int = 8
    # Ground-truth receptive-field centres, one (x, y) in centered dva per
    # channel. None lays the channels out deterministically on a spiral, so
    # a quick simulation needs no hand-placed geometry.
    sim_rf_centers_dva: tuple[tuple[float, float], ...] | None = None
    sim_rf_sigma_dva: float = 1.5
    sim_baseline_hz: float = 3.0
    sim_peak_hz: float = 60.0
    sim_latency_ms: float = 40.0
    sim_duration_ms: float = 80.0
    # The stimulus event a simulated channel responds to. Its payload must
    # carry the flash position as ``x_dva``/``y_dva`` (the RF-mapping
    # template's PROBE_ON does). Validated against the experiment's own
    # event schema at session build, like every config key that names one.
    sim_respond_to: str = "PROBE_ON"
    sim_seed: int = 0

    # --- sorted_stream -------------------------------------------------
    # The sorter's ZeroMQ PUB endpoint. It publishes; alhazen subscribes.
    address: str = "tcp://127.0.0.1:5556"
    # How long the stream may go completely silent before that is a fault.
    # A sorter that stopped publishing must never read as "the neurons went
    # quiet", so the contract asks it to heartbeat at least every 200 ms and
    # this is the ten-fold margin on that.
    heartbeat_timeout_ms: float = 2000.0

    @model_validator(mode="after")
    def _valid(self) -> SpikeSourceConfig:
        self._reject_other_backend_fields()
        if not 1024 <= self.port <= 65535:
            raise ValueError("spikes port must be between 1024 and 65535")
        if not re.match(_ZMQ_ADDRESS_RE, self.address):
            raise ValueError(
                f"spikes address {self.address!r} must be a ZeroMQ endpoint with a "
                f"transport, e.g. 'tcp://192.168.1.50:5556'"
            )
        if self.heartbeat_timeout_ms <= 0:
            raise ValueError("spikes heartbeat_timeout_ms must be > 0")
        if not re.match(_CHANNELS_RE, self.channels):
            raise ValueError(
                f"spikes channels {self.channels!r} must be 'all' or comma-separated "
                f"'N' / 'N:M' entries, e.g. '0:383' or '0,5,9'"
            )
        if not re.match(r"^(imec\d+|nidq|obx\d+)$", self.stream):
            raise ValueError(f"spikes stream {self.stream!r} must be 'imec<N>', 'obx<N>' or 'nidq'")
        for name in ("fetch_interval_ms", "threshold_sigmas", "hp_window_ms"):
            if getattr(self, name) <= 0:
                raise ValueError(f"spikes {name} must be > 0")
        if self.refractory_ms < 0:
            raise ValueError("spikes refractory_ms must be >= 0")
        if self.sim_channels < 1:
            raise ValueError("spikes sim_channels must be >= 1")
        if self.sim_rf_centers_dva is not None and len(self.sim_rf_centers_dva) != (
            self.sim_channels
        ):
            raise ValueError(
                f"spikes sim_rf_centers_dva lists {len(self.sim_rf_centers_dva)} centres for "
                f"sim_channels={self.sim_channels} — one (x, y) per simulated channel"
            )
        for name in ("sim_rf_sigma_dva", "sim_peak_hz", "sim_duration_ms"):
            if getattr(self, name) <= 0:
                raise ValueError(f"spikes {name} must be > 0")
        for name in ("sim_baseline_hz", "sim_latency_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"spikes {name} must be >= 0")
        return self

    def _reject_other_backend_fields(self) -> None:
        # model_fields_set holds only the keys the YAML actually supplied,
        # so untouched defaults for the other backends stay silent while a
        # value someone typed on purpose is refused by name.
        every = set().union(*BACKEND_FIELDS.values())
        wrong = every - set(BACKEND_FIELDS[self.backend])
        named = sorted(wrong & self.model_fields_set)
        if named:
            raise ValueError(
                f"spikes backend {self.backend!r} ignores {', '.join(named)} — "
                f"remove {'them' if len(named) > 1 else 'it'}, or change the backend"
            )


class DevicesConfig(Model):
    """Which device classes this rig has. ``None`` means "absent on this
    rig" — a display-only rig (a laptop, a psychophysics booth with no
    tracker) leaves them all None and every device seam stays unwired."""

    eyetracker: EyeTrackerConfig | None = None
    reward: RewardHwConfig | None = None
    sync: SyncHwConfig | None = None
    recording: RecordingConfig | None = None
    spikes: SpikeSourceConfig | None = None


class RigConfig(Model):
    """One physical machine: its display, its devices, and where data lands."""

    monitor: MonitorConfig
    display: DisplayConfig = DisplayConfig()
    dashboard: DashboardConfig = DashboardConfig()
    database: DatabaseConfig = DatabaseConfig()
    devices: DevicesConfig = DevicesConfig()
    data_root: Path


class SessionInfo(Model):
    """Identity of one recorded run, stamped into the snapshot and filenames."""

    subject: str
    session: int
    run: int
    task_name: str
    seed: int  # always the resolved concrete seed, never None

    @model_validator(mode="after")
    def _valid(self) -> SessionInfo:
        if not self.subject.isalnum():
            raise ValueError("subject id must be alphanumeric (it becomes a filename segment)")
        for name in ("session", "run"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        ok = self.task_name and all(c.isalnum() or c == "-" for c in self.task_name)
        if not ok or not self.task_name.islower():
            raise ValueError("task_name must be lowercase alphanumeric/hyphen (filename segment)")
        return self


class SessionConfig(Model):
    """The fully-merged configuration one session runs: rig + task params +
    identity + provenance of where each layer came from. This is what the
    snapshot serializes (config/snapshot.py adds environment provenance)."""

    rig: RigConfig
    info: SessionInfo
    # Task parameters are validated by the *task's own* pydantic model before
    # they get here (config/loader.load_params); stored as plain data so the
    # snapshot round-trips without importing experiment code.
    task_params: dict[str, object]
    sources: dict[str, str]  # which file/origin supplied each layer


def resolve_refresh(nominal_hz: float, measured_hz: float, tolerance_hz: float) -> float:
    """The rate frame math uses: the measured one — after checking it agrees
    with the nominal rate. Divergence means the OS/panel is not doing what
    the rig config promises, which invalidates every frame-denominated
    duration, so it fails loudly here rather than corrupting timing silently.
    """
    if math.isnan(measured_hz) or measured_hz <= 0:
        raise ConfigError("measured refresh rate is invalid; display warm-up failed")
    if abs(measured_hz - nominal_hz) > tolerance_hz:
        raise ConfigError(
            f"measured refresh {measured_hz:.2f} Hz disagrees with the rig config's nominal "
            f"{nominal_hz:.2f} Hz by more than {tolerance_hz} Hz — fix the display mode or "
            f"the rig config before collecting data"
        )
    return measured_hz
