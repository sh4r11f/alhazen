"""Live spike sources: a neural stream read *during* the session.

``devices/recording.py`` answers "where will the recording's files be
afterwards"; this module answers "what is the probe seeing right now". One
protocol, one real backend, one simulated sibling, a factory — the same
shape as every other device seam (architecture §4, §11):

- :class:`SpikeGLXLiveSource` connects to SpikeGLX's remote command server
  through the official SpikeGLX-CPP-SDK Python bindings, fetches the raw
  stream in a background thread, and turns it into threshold-crossing
  spikes with :mod:`alhazen.neural.detect`. The SDK is imported lazily
  inside ``connect()`` — it ships with the SDK, not on PyPI — so ``import
  alhazen`` and the whole test suite work without it.
- :class:`SimulatedSpikeSource` invents spikes from configured ground-truth
  receptive fields. It is a bus subscriber (like the automated response
  device): a stimulus event carrying ``x_dva``/``y_dva`` in its payload
  makes each simulated channel fire by its distance from that channel's RF
  centre. That is what lets the whole live pipeline — detector aside — run
  end to end, and be *asserted on* (known RF in, same RF out), with no
  hardware anywhere.

The consumer's contract is ``drain()``: everything detected since the last
drain, with times already on the **session clock** (the device owns the
stream→session mapping, ``neural/timebase.py``, exactly as the eye trackers
own theirs), plus ``covered_until`` — the session time up to which detection
is *complete*. A consumer counting spikes in a window must wait until
``covered_until`` passes the window's end; counting earlier would silently
undercount the newest flashes, which is the quiet kind of wrong this
framework exists to refuse.

Threading: the fetch thread touches shared state only under one lock, and
never touches the display, the bus, or the engine. A fault in the thread is
stored and re-raised from the next ``drain()`` call — on the session's own
thread, loudly — because a dead stream must end up in front of the
experimenter, not in a log nobody is watching.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np

from alhazen.config.models import SpikeSourceConfig
from alhazen.core.clock import Clock
from alhazen.core.events import Event
from alhazen.errors import SpikeSourceError
from alhazen.neural.detect import CAR_MIN_CHANNELS, SpikeDetector
from alhazen.neural.timebase import StreamTimebase

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

log = logging.getLogger(__name__)

# SpikeGLX stream-type codes for the remote API's (js, ip) addressing:
# js 0 = the NI-DAQ stream, 1 = a OneBox, 2 = an imec probe; ip counts
# substreams within the type (probe 0, probe 1, ...).
_JS_NI, _JS_OBX, _JS_IMEC = 0, 1, 2

# Never ask the server for more than a second of samples in one fetch: a
# session that fell behind catches up in bounded bites instead of one
# giant allocation.
_MAX_FETCH_S = 1.0


class SpikeBatch:
    """What one ``drain()`` returns: spikes on the session clock.

    ``times`` (float64 seconds) and ``channels`` (int32) are parallel
    arrays, time-ordered. ``channels`` holds dense ROW indices — 0 to
    ``n_channels − 1``, positions in the source's monitored set — never the
    hardware channel numbers, so a consumer can histogram them directly
    however sparse the monitored subset is; ``SpikeSource.channel_ids`` maps
    a row back to its hardware id for labelling. ``covered_until`` is the
    session time up to which detection is complete — None while the stream
    has produced nothing yet.
    """

    def __init__(
        self, times: np.ndarray, channels: np.ndarray, covered_until: float | None
    ) -> None:
        if times.shape != channels.shape:
            raise ValueError(
                f"times {times.shape} and channels {channels.shape} must be parallel arrays"
            )
        self.times = times
        self.channels = channels
        self.covered_until = covered_until

    def __len__(self) -> int:
        return int(self.times.size)


@runtime_checkable
class SpikeSource(Protocol):
    """The seam a live analysis consumes; backends live behind it."""

    @property
    def n_channels(self) -> int: ...

    @property
    def channel_ids(self) -> tuple[int, ...]:
        """Hardware channel id per monitored row, for labelling maps."""
        ...

    def configure(self, clock: Clock) -> None:
        """Receive the session clock (every backend stamps on it — the same
        rule the eye trackers follow, and for the same reason: one clock)."""
        ...

    def connect(self) -> None:
        """Open the stream; loud, typed errors at session build time."""
        ...

    def start(self) -> None:
        """Begin producing. Separate from connect() so check-rig can prove
        the connection without starting a background thread."""
        ...

    def drain(self) -> SpikeBatch:
        """Everything detected since the last drain. Re-raises any fault the
        background work hit, on the caller's thread."""
        ...

    def describe(self) -> str:
        """One line for logs and check-rig: what is being read, from where."""
        ...

    def close(self) -> None: ...


# ----------------------------------------------------------------------
# Channel-list parsing
# ----------------------------------------------------------------------


def parse_channels(spec: str, n_available: int) -> list[int]:
    """``"0:383"`` / ``"0,5,9"`` / ``"all"`` → explicit channel indices.

    Ranges are inclusive at both ends, matching how electrophysiologists
    write them. Order is preserved, duplicates are refused — a channel
    listed twice would silently double its spike counts downstream.
    """
    if spec == "all":
        return list(range(n_available))
    channels: list[int] = []
    for entry in spec.split(","):
        first, _, last = entry.partition(":")
        lo = int(first)
        hi = int(last) if last else lo
        if hi < lo:
            raise SpikeSourceError(f"spikes channels range {entry!r} runs backwards")
        channels.extend(range(lo, hi + 1))
    if len(set(channels)) != len(channels):
        raise SpikeSourceError(f"spikes channels {spec!r} names a channel twice")
    out_of_range = [c for c in channels if not 0 <= c < n_available]
    if out_of_range:
        raise SpikeSourceError(
            f"spikes channels {spec!r} names channels {out_of_range[:6]} but the stream "
            f"has {n_available} (0..{n_available - 1})"
        )
    return channels


def _parse_stream(stream: str) -> tuple[int, int]:
    """``"imec0"``/``"nidq"``/``"obx1"`` → the remote API's (js, ip) pair."""
    if stream == "nidq":
        return _JS_NI, 0
    if stream.startswith("imec"):
        return _JS_IMEC, int(stream[len("imec") :])
    if stream.startswith("obx"):
        return _JS_OBX, int(stream[len("obx") :])
    # The config model already validated the shape; reaching this is a bug.
    raise SpikeSourceError(f"unrecognized stream name {stream!r}")


# ----------------------------------------------------------------------
# The SpikeGLX remote connection, isolated from the rest of the backend
# ----------------------------------------------------------------------


class _SglxConnection:
    """Every ctypes call to the vendor SDK, in one small class.

    Isolated so the fetch loop above it is plain Python over numpy arrays —
    which is what lets the loop be tested against a fake connection, with
    the vendor surface reduced to the handful of calls actually used.
    """

    def __init__(self, host: str, port: int) -> None:
        self._sglx = _import_sglx()
        self._handle = self._sglx.c_sglx_createHandle()
        if not self._sglx.c_sglx_connect(self._handle, host.encode(), port):
            error = self._error()
            self.close()
            raise SpikeSourceError(
                f"could not connect to SpikeGLX at {host}:{port} ({error}) — check that "
                f"SpikeGLX is running and its command server is enabled "
                f"(Options > Command Server Settings)"
            )

    def _error(self) -> str:
        raw = self._sglx.c_sglx_getError(self._handle)
        return raw.decode(errors="replace") if raw else "no error text"

    def version(self) -> str:
        raw = self._sglx.c_sglx_getVersion(self._handle)
        return raw.decode(errors="replace") if raw else "unknown"

    def is_running(self) -> bool:
        from ctypes import byref, c_bool

        running = c_bool()
        if not self._sglx.c_sglx_isRunning(byref(running), self._handle):
            raise SpikeSourceError(f"SpikeGLX isRunning query failed ({self._error()})")
        return bool(running.value)

    def sample_rate(self, js: int, ip: int) -> float:
        rate = float(self._sglx.c_sglx_getStreamSampleRate(self._handle, js, ip))
        if rate <= 0:
            raise SpikeSourceError(
                f"SpikeGLX reports no sample rate for stream (js={js}, ip={ip}) "
                f"({self._error()}) — is that stream enabled in this run?"
            )
        return rate

    def sample_count(self, js: int, ip: int) -> int:
        return int(self._sglx.c_sglx_getStreamSampleCount(self._handle, js, ip))

    def acq_channel_counts(self, js: int, ip: int) -> list[int]:
        """The stream's acquired-channel counts by type — [AP, LF, SY] for an
        imec stream, [MN, MA, XA, DW] for the NI stream."""
        from ctypes import byref, c_int

        nval = c_int()
        if not self._sglx.c_sglx_getStreamAcqChans(byref(nval), self._handle, js, ip):
            raise SpikeSourceError(
                f"SpikeGLX channel-count query failed for stream (js={js}, ip={ip}) "
                f"({self._error()})"
            )
        return [int(self._sglx.c_sglx_getint(self._handle, i)) for i in range(nval.value)]

    def fetch(
        self, js: int, ip: int, start: int, max_samps: int, channels: list[int]
    ) -> tuple[int, np.ndarray]:
        """Samples from ``start``: (index of the first returned sample, an
        owned ``(n_samples, n_channels)`` int16 array)."""
        from ctypes import POINTER, byref, c_int, c_short, c_ulonglong

        data = POINTER(c_short)()
        n_data = c_int()
        subset = (c_int * len(channels))(*channels)
        head = int(
            self._sglx.c_sglx_fetch(
                byref(data),
                byref(n_data),
                self._handle,
                js,
                ip,
                c_ulonglong(start),
                max_samps,
                subset,
                len(channels),
                1,  # downsample 1: every sample — detection needs them all
            )
        )
        if head == 0:
            raise SpikeSourceError(f"SpikeGLX fetch failed ({self._error()})")
        count = int(n_data.value)
        if count % len(channels) != 0:
            raise SpikeSourceError(
                f"SpikeGLX fetch returned {count} values for {len(channels)} channels — "
                f"not a whole number of samples"
            )
        # Copy immediately: the buffer belongs to the SDK and is reused by
        # the next call.
        flat = np.ctypeslib.as_array(data, shape=(count,)).copy()
        return head, flat.reshape(count // len(channels), len(channels))

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle is not None:
            self._sglx.c_sglx_close(handle)
            self._sglx.c_sglx_destroyHandle(handle)


def _import_sglx() -> Any:
    """The official bindings, or an error naming where they ship.

    The SpikeGLX remote API's Python bindings are ctypes over the SDK's own
    compiled library, distributed with the SpikeGLX-CPP-SDK — not on PyPI.
    Importing them also loads that library, so an ImportError and an
    OSError both mean the same thing to the experimenter: the SDK is not
    (fully) installed on this machine.
    """
    try:
        from sglx_pkg import sglx  # the SDK's own package layout

        return sglx
    except (ImportError, OSError):
        pass
    try:
        import sglx  # a bare sglx.py placed on the path

        return sglx
    except (ImportError, OSError) as error:
        raise SpikeSourceError(
            "the SpikeGLX remote API bindings are not available — they ship with the "
            "SpikeGLX-CPP-SDK (github.com/billkarsh/SpikeGLX-CPP-SDK), not on PyPI. Put its "
            "Python/sglx_pkg package on this machine's PYTHONPATH with the built SglxApi "
            "library beside it, or use spikes backend 'simulated'."
        ) from error


# ----------------------------------------------------------------------
# The real backend
# ----------------------------------------------------------------------


class SpikeGLXLiveSource:
    """Threshold-crossing spikes from a running SpikeGLX acquisition.

    ``connect()`` proves everything at session build time — server
    reachable, a run actually acquiring, the stream present, the channel
    list valid — so a mis-set rig fails before a subject is in the chair.
    ``start()`` then begins the background fetch loop.
    """

    def __init__(
        self,
        cfg: SpikeSourceConfig,
        connection_factory: Callable[[], _SglxConnection] | None = None,
    ) -> None:
        self._cfg = cfg
        self._js, self._ip = _parse_stream(cfg.stream)
        # Injectable for tests only: production always builds the real
        # connection. The seam is the connection, not the loop, so the loop
        # (cursor, gaps, timebase, detector, faults) is tested for real.
        self._connect_factory = connection_factory or (lambda: _SglxConnection(cfg.host, cfg.port))
        self._connection: _SglxConnection | None = None
        self._clock: Clock | None = None
        self._detector: SpikeDetector | None = None
        self._timebase: StreamTimebase | None = None
        self._channels: list[int] = []
        self._rate = 0.0
        self._cursor = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Guarded by _lock:
        self._pending: list[tuple[np.ndarray, np.ndarray]] = []  # (session times, channels)
        self._covered: float | None = None
        self._fault: BaseException | None = None
        self._gap_samples = 0

    @property
    def n_channels(self) -> int:
        return len(self._channels)

    @property
    def channel_ids(self) -> tuple[int, ...]:
        return tuple(self._channels)

    def configure(self, clock: Clock) -> None:
        self._clock = clock

    def connect(self) -> None:
        cfg = self._cfg
        connection = self._connect_factory()
        try:
            if not connection.is_running():
                raise SpikeSourceError(
                    f"SpikeGLX at {cfg.host}:{cfg.port} is connected but no acquisition is "
                    f"running — start the run in SpikeGLX before starting the session"
                )
            self._rate = connection.sample_rate(self._js, self._ip)
            counts = connection.acq_channel_counts(self._js, self._ip)
            if cfg.channels == "all":
                if self._js != _JS_IMEC:
                    raise SpikeSourceError(
                        f"spikes channels 'all' is only defined for an imec stream (it means "
                        f"the AP channels); name the channels explicitly for {cfg.stream!r}"
                    )
                # An imec stream acquires [AP, LF, SY]; the spikes live on AP.
                n_neural = counts[0]
            else:
                # The subset indexes the acquired channel table, whatever the
                # stream type; the total is what bounds it.
                n_neural = sum(counts)
            self._channels = parse_channels(cfg.channels, n_neural)
            if cfg.car and len(self._channels) < CAR_MIN_CHANNELS:
                # The detector would refuse this anyway; refused here with
                # the rig config's own vocabulary, before a session build
                # gets any further.
                raise SpikeSourceError(
                    f"spikes.car needs at least {CAR_MIN_CHANNELS} monitored channels for an "
                    f"unbiased noise estimate; channels {cfg.channels!r} selects "
                    f"{len(self._channels)} — monitor more channels or set car: false"
                )
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._detector = SpikeDetector(
            n_channels=len(self._channels),
            rate_hz=self._rate,
            threshold_sigmas=cfg.threshold_sigmas,
            hp_window_ms=cfg.hp_window_ms,
            refractory_ms=cfg.refractory_ms,
            car=cfg.car,
        )
        self._timebase = StreamTimebase(self._rate)
        # Start reading at "now": the history before the session is the
        # recording's business, not the live map's.
        self._cursor = connection.sample_count(self._js, self._ip)
        log.info("spikes: %s", self.describe())

    def start(self) -> None:
        if self._connection is None:
            raise SpikeSourceError("spike source started before connect()")
        if self._clock is None:
            raise SpikeSourceError("spike source started before configure(clock)")
        if self._thread is not None:
            raise SpikeSourceError("spike source started twice")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="alhazen-spikes", daemon=True)
        self._thread.start()

    # -- the background loop -------------------------------------------

    def _run(self) -> None:
        interval_s = self._cfg.fetch_interval_ms / 1000.0
        try:
            while not self._stop.is_set():
                self._poll_once()
                self._stop.wait(interval_s)
        except BaseException as error:  # stored, re-raised on the session thread
            log.exception("spike fetch thread failed")
            with self._lock:
                self._fault = error

    def _poll_once(self) -> None:
        assert self._connection is not None and self._clock is not None
        assert self._detector is not None and self._timebase is not None
        connection, clock = self._connection, self._clock
        detector, timebase = self._detector, self._timebase

        available = connection.sample_count(self._js, self._ip)
        if available <= self._cursor:
            # No new samples, but a fresh simultaneous reading of both
            # clocks — worth noting, since the offset estimator lives on
            # exactly these observations.
            with self._lock:
                timebase.note_fetch(available, clock.now())
            return

        max_fetch = max(1, int(self._rate * _MAX_FETCH_S))
        while available > self._cursor and not self._stop.is_set():
            want = min(available - self._cursor, max_fetch)
            head, chunk = connection.fetch(self._js, self._ip, self._cursor, want, self._channels)
            t_after = clock.now()
            if head > self._cursor:
                # The server's ring buffer moved past our cursor: samples
                # are gone for the live map (the recording still has them).
                # Counted and logged, and the detector's continuity state is
                # reset so it cannot stitch across the hole.
                lost = head - self._cursor
                self._gap_samples += lost
                log.warning(
                    "spike stream fell behind: lost %d samples (%.0f ms) to the ring buffer",
                    lost,
                    1000.0 * lost / self._rate,
                )
                detector.reset()
            samples, channel_rows = detector.process(chunk, head)
            self._cursor = head + chunk.shape[0]
            with self._lock:
                timebase.note_fetch(self._cursor, t_after)
                if samples.size:
                    # Mapped to session seconds here, with the offset the
                    # timebase holds *now*. Channel values stay ROW indices
                    # (0..n_channels-1); channel_ids carries the hardware
                    # numbers for anything that labels a map.
                    self._pending.append((timebase.to_session(samples), channel_rows))
                if detector.emitted_until >= 0:
                    self._covered = timebase.sample_to_session(detector.emitted_until)

    # -- the session-side surface --------------------------------------

    def drain(self) -> SpikeBatch:
        with self._lock:
            if self._fault is not None:
                fault, self._fault = self._fault, None
                raise SpikeSourceError(f"the spike fetch thread died: {fault}") from fault
            if self._thread is not None and not self._thread.is_alive() and not self._stop.is_set():
                raise SpikeSourceError(
                    "the spike fetch thread is no longer running and left no error — "
                    "the live map has stopped updating"
                )
            batches, self._pending = self._pending, []
            covered = self._covered
        if not batches:
            return SpikeBatch(np.empty(0, dtype=np.float64), np.empty(0, dtype=np.int32), covered)
        times = np.concatenate([t for t, _ in batches])
        channels = np.concatenate([c for _, c in batches])
        order = np.argsort(times, kind="stable")
        return SpikeBatch(times[order], channels[order], covered)

    def describe(self) -> str:
        return (
            f"spikeglx at {self._cfg.host}:{self._cfg.port} — {self._cfg.stream} @ "
            f"{self._rate:g} Hz, {len(self._channels)} channels, threshold "
            f"{self._cfg.threshold_sigmas:g}σ"
        )

    def close(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
            if thread.is_alive():  # pragma: no cover - a hung socket
                log.error("spike fetch thread did not stop within 2 s; abandoning it")
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()


# ----------------------------------------------------------------------
# The simulated sibling
# ----------------------------------------------------------------------


class SimulatedSpikeSource:
    """Ground-truth receptive fields, spiking to the session's own stimuli.

    A bus subscriber: when the configured stimulus event fires, each
    simulated channel emits a Poisson burst scaled by the Gaussian distance
    between the flash and that channel's RF centre, after a fixed latency.
    Between stimuli every channel ticks along at its baseline rate. Seeded,
    so a test can assert the recovered map's peak, not just its existence.
    """

    def __init__(self, cfg: SpikeSourceConfig) -> None:
        self._cfg = cfg
        self._rng = np.random.default_rng(cfg.sim_seed)
        self._clock: Clock | None = None
        if cfg.sim_rf_centers_dva is not None:
            self._centers = np.asarray(cfg.sim_rf_centers_dva, dtype=np.float64)
        else:
            self._centers = _spiral_layout(cfg.sim_channels)
        # Spikes whose time is still in the future (latency puts them
        # there); released by drain() once the clock passes them.
        self._future_times: list[np.ndarray] = []
        self._future_channels: list[np.ndarray] = []
        self._baseline_from: float | None = None

    @property
    def n_channels(self) -> int:
        return self._cfg.sim_channels

    @property
    def channel_ids(self) -> tuple[int, ...]:
        return tuple(range(self.n_channels))

    @property
    def rf_centers_dva(self) -> np.ndarray:
        """The ground truth, ``(n_channels, 2)`` — what a test asserts the
        recovered map against."""
        return self._centers.copy()

    def configure(self, clock: Clock) -> None:
        self._clock = clock

    def connect(self) -> None:
        return  # nothing to open

    def start(self) -> None:
        if self._clock is None:
            raise SpikeSourceError("simulated spike source started before configure(clock)")
        self._baseline_from = self._clock.now()

    def on_event(self, event: Event) -> None:
        """The bus subscription: respond to the configured stimulus event."""
        if event.name != self._cfg.sim_respond_to:
            return
        x = event.payload.get("x_dva")
        y = event.payload.get("y_dva")
        if x is None or y is None:
            # Configured to respond to an event that carries no position:
            # that is a wiring mistake, and inventing a flat response for it
            # would draw a map of nothing and call it data.
            raise SpikeSourceError(
                f"simulated spike source responds to {event.name!r}, but its payload has no "
                f"x_dva/y_dva — point sim_respond_to at an event that carries the flash "
                f"position"
            )
        cfg = self._cfg
        duration_s = cfg.sim_duration_ms / 1000.0
        distance_sq = ((self._centers - np.array([x, y])) ** 2).sum(axis=1)
        rates = cfg.sim_peak_hz * np.exp(-distance_sq / (2.0 * cfg.sim_rf_sigma_dva**2))
        counts = self._rng.poisson(rates * duration_s)
        total = int(counts.sum())
        if total == 0:
            return
        start = event.t + cfg.sim_latency_ms / 1000.0
        times = start + self._rng.uniform(0.0, duration_s, size=total)
        channels = np.repeat(np.arange(self.n_channels, dtype=np.int32), counts)
        self._future_times.append(times)
        self._future_channels.append(channels)

    def drain(self) -> SpikeBatch:
        if self._clock is None or self._baseline_from is None:
            raise SpikeSourceError("simulated spike source drained before start()")
        now = self._clock.now()

        times_parts: list[np.ndarray] = []
        channel_parts: list[np.ndarray] = []

        # Baseline firing over the interval since the last drain.
        elapsed = max(0.0, now - self._baseline_from)
        if elapsed > 0 and self._cfg.sim_baseline_hz > 0:
            counts = self._rng.poisson(self._cfg.sim_baseline_hz * elapsed, self.n_channels)
            total = int(counts.sum())
            if total:
                times_parts.append(self._baseline_from + self._rng.uniform(0, elapsed, total))
                channel_parts.append(np.repeat(np.arange(self.n_channels, dtype=np.int32), counts))
        self._baseline_from = now

        # Evoked spikes whose time has arrived; the rest stay queued.
        held_times: list[np.ndarray] = []
        held_channels: list[np.ndarray] = []
        for times, channels in zip(self._future_times, self._future_channels, strict=True):
            due = times <= now
            if due.any():
                times_parts.append(times[due])
                channel_parts.append(channels[due])
            if (~due).any():
                held_times.append(times[~due])
                held_channels.append(channels[~due])
        self._future_times, self._future_channels = held_times, held_channels

        if not times_parts:
            return SpikeBatch(np.empty(0, np.float64), np.empty(0, np.int32), now)
        times = np.concatenate(times_parts)
        channels = np.concatenate(channel_parts)
        order = np.argsort(times, kind="stable")
        return SpikeBatch(times[order], channels[order], now)

    def describe(self) -> str:
        return (
            f"simulated — {self.n_channels} channels with ground-truth RFs "
            f"(σ {self._cfg.sim_rf_sigma_dva:g} dva, peak {self._cfg.sim_peak_hz:g} Hz, "
            f"responding to {self._cfg.sim_respond_to})"
        )

    def close(self) -> None:
        return  # nothing was opened


def _spiral_layout(n: int) -> np.ndarray:
    """Deterministic RF centres when none are configured: a golden-angle
    spiral out to ~6 dva, so any number of channels lands spread out rather
    than stacked — a simulation whose channels all share one centre cannot
    show that the per-channel maps differ."""
    index = np.arange(n)
    radius = 6.0 * np.sqrt((index + 0.5) / n)
    angle = index * 2.399963  # the golden angle, radians
    return np.column_stack([radius * np.cos(angle), radius * np.sin(angle)])


def make_spikes(cfg: SpikeSourceConfig) -> SpikeSource:
    """Construct the spike source a rig config names. Shared by session
    build and ``check-rig``, so a clean check exercises the real
    constructor."""
    if cfg.backend == "spikeglx":
        return SpikeGLXLiveSource(cfg)
    return SimulatedSpikeSource(cfg)
