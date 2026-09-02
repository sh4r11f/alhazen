# Live spikes and live analysis

*A neural stream read during the session, and the seam that lets a task
compute on it between trials. These are framework capabilities — any
experiment can use them; the worked example is the
[rf-mapping](https://github.com/sh4r11f/rf-mapping) experiment, which
turns them into live receptive-field maps.*

## The spike source: `devices.spikes`

`devices/recording.py` answers "where will the recording's files be
afterwards"; the spike source answers "what is the probe seeing right
now". One protocol (`SpikeSource`), one real backend, one simulated
sibling, a factory — the same shape as every device seam (architecture
§4):

```yaml
devices:
  spikes:
    backend: spikeglx
    host: 192.168.1.50     # the acquisition machine
    port: 4142             # SpikeGLX: Options > Command Server Settings
    stream: imec0          # imec<N>, obx<N>, or nidq
    channels: all          # or "0:383", "0:127,256:383", "5,9,12"
    fetch_interval_ms: 200
    threshold_sigmas: 4.5
    car: true              # needs ≥16 monitored channels; refused otherwise
```

A background thread fetches the stream, `alhazen.neural.detect` turns it
into threshold crossings (optional median common-average reference, a
moving-average high-pass, −kσ crossings against a robust noise estimate,
chunk-boundary-safe), and `alhazen.neural.timebase` places them on the
**session clock**. The consumer's contract is `drain()`: everything
detected since the last drain, plus `covered_until` — the session time up
to which detection is *complete*, so a consumer counting spikes in a
window can wait rather than silently undercount the newest events. A
fault in the fetch thread re-raises from the next `drain()` on the
session's own thread: a silently dead stream would read as "the neurons
stopped responding", which is a scientific claim, not a connection
status.

**The clock mapping, honestly.** Live alignment is a minimum-offset
estimate over recent fetches — sub-millisecond on a lab LAN, a few
milliseconds worst case, drift bounded by a sliding window. Fine inside
the tens-of-milliseconds windows a live analysis counts in; not what a
spike-timing analysis should use. The offline path aligns with the TTL
pulses (`alhazen.analysis.sync`), as always.

**The SDK.** The `spikeglx` backend uses the official SpikeGLX-CPP-SDK
Python bindings (`sglx_pkg`), which ship with the SDK rather than on
PyPI — put its `Python/sglx_pkg` package on the rig's `PYTHONPATH` with
the built `SglxApi` library beside it. The backend names exactly this if
the import fails. `connect()` runs at session build and refuses a
SpikeGLX that is reachable but not acquiring; `alhazen check-rig` performs
the same connection and reports the stream, rate and channel count.

**The simulated sibling** runs the whole pipeline with no hardware. It is
a bus subscriber: when the configured stimulus event fires, each
simulated channel emits a Poisson burst scaled by the Gaussian distance
between the event's payload position (`x_dva`/`y_dva`) and that channel's
ground-truth receptive-field centre:

```yaml
devices:
  spikes:
    backend: simulated
    sim_channels: 6
    sim_respond_to: PROBE_ON        # validated against the task's events
    # sim_rf_centers_dva: [[-4.0, -3.0], [3.0, 2.0], ...]  # or auto-spread
    sim_rf_sigma_dva: 1.5
    sim_peak_hz: 120.0
    sim_baseline_hz: 3.0
```

Seeded and deterministic, which is what lets a test assert *known field
in, same field out* rather than "a map appeared". Simulate mode counts a
real spike source as hardware and refuses it, exactly as it refuses a
real tracker.

## Sorted streams: `backend: sorted_stream`

The `spikeglx` backend detects its own threshold crossings, so its rows
are *channels*. When a real-time spike sorter is already running, the
useful unit of analysis is a *unit*, and this backend subscribes to one:

```yaml
devices:
  spikes:
    backend: sorted_stream
    address: tcp://192.168.1.50:5556   # the sorter's ZeroMQ PUB endpoint
    fetch_interval_ms: 20              # how often the reader thread polls
    heartbeat_timeout_ms: 2000         # silence past this is a fault
```

Install the transport with `pip install 'alhazen-vision[zmq]'`; the
backend imports `zmq` lazily and names this extra if it is missing.

**Rows are units, and the row map only grows.** A unit first seen in the
tenth minute of a session gets a new row rather than renumbering the
existing ones, so a consumer's per-row history stays valid for the whole
session. `channel_ids` maps a row back to its unit id. A unit that spikes
before it has been announced still gets a row, because dropping it would
undercount the newest unit at exactly the moment it appears.

### The wire contract

The sorter is a ZeroMQ `PUB` socket; alhazen subscribes. Every message is
a multipart frame whose first frame is a JSON header. Three types:

```jsonc
// "units" — MUST be the first message, and repeated whenever the set changes.
{"type": "units", "unit_ids": [3, 9, 14], "labels": ["good", "good", "mua"],
 "sample_rate_hz": 30000}

// "spikes" — three frames: this header, then k int64 sample indices,
// then k int32 unit ids.
{"type": "spikes", "stream": "imec0", "covered_until_sample": 90300, "n": 2,
 "seq": 41}

// "heartbeat" — at least every 200 ms, so silence is distinguishable
// from a quiet brain.
{"type": "heartbeat", "covered_until_sample": 90600, "seq": 42}
```

Four rules the consumer enforces, each because the alternative fails
quietly:

- **The sample rate rides on `units`, which must come first.** Nothing can
  be placed on a clock without it, and a timebase built on a rate of zero
  would stack every spike at one instant. A `spikes` or `heartbeat`
  arriving before any `units` is refused by name, and so is a rate that
  changes mid-session.
- **`covered_until_sample` is on every timed message**, including
  heartbeats. It is what lets a consumer wait for a window to be complete
  instead of counting early, and a silent stretch must still make
  progress.
- **`seq` is optional but strongly recommended.** A `PUB` socket discards
  messages for a slow subscriber without telling either end, so a lost
  `spikes` message would otherwise just undercount a decoder's features.
  Given a sequence number, the source counts the gap, logs it, and reports
  it in `describe()`; without one it says `drops undetectable (no seq)`,
  because "no drops seen" and "cannot see drops" are different claims.
- **`n` must match the array frames.** A header disagreeing with its own
  payload is refused rather than truncated.

`alhazen check-rig` **listens** for this backend rather than only opening
the socket: a SUB socket connects successfully to an endpoint nobody is
publishing on, so a connect-only check would give a dead sorter a clean
bill of health. It waits up to `heartbeat_timeout_ms` for a `units`
message and reports the unit count, the sample rate and the covered-until
lag — which is the number that decides whether a between-trials decode can
finish in time.

## The live-analysis seam: `Task.live_analysis`

Architecture §5.5 states the contract; the shape of an implementation:

```python
class MyLiveAnalysis:
    def on_event(self, event):        # optional; bus-called MID-TRIAL:
        ...                           # take notes only, never compute

    def on_trial(self, record):       # between trials: the real work
        batch = self.spikes.drain()   # times on the session clock
        ...

    def panels(self):                 # finished dashboard payloads
        return [{"title": "...", "section": "...", "data": {...}}]

    def finish(self, run_dir):        # teardown, before the manifest:
        ...                           # save the artifact
```

The builder wires it like a device: the task's hook receives a
`LiveWiring` carrying the spike source the *rig config* built (or None —
say so on a panel, never crash and never stay quiet), the screen and the
session clock. The runner drives `on_trial` after each scored row is
written and before the dashboard publish, so the panels in that publish
already include the trial; `finish` runs before the manifest is written
and before the spike source closes, so the artifact is hashed and one
last drain is possible.

## The `heatmap` panel form

Live analyses introduced one wire form of their own, drawn by the
dashboard page like every other (dashboard.md): one or many cell matrices
on a shared colour scale — small multiples with a single colourbar,
because per-map scales would quietly break the comparison — cells at the
data's own aspect ratio, and `null` cells drawn muted as *not measured
yet*, never as zero. The scale interpolates the theme's own ordinal ramp,
so it follows light and dark like every other mark.
