# Experiment database

Every session writes its immutable CSV/YAML/native-device artifacts as before
and mirrors them into `experiment.sqlite3` at the rig's `data_root`. One
database therefore contains every subject, session, run and task for that
experiment. SQLite needs no server and can be opened by Python, R, Julia,
MATLAB, Datasette, DBeaver, or the `sqlite3` command-line tool.

The normalized tables are `runs`, `subjects`, `trials`, `events`, `frames`,
`frame_inputs`, `paradigm_rows`, `device_streams`, `device_samples`,
`training_states`, and `artifacts`.

## What identifies a run

`run_id` is `sub-<ID>/ses-<NNN>/run-<NN>/task-<name>/<YYYYMMDD>`. The **date**
is part of it because it is part of what makes a run unique on disk: a run
directory is named for the subject, session and run number only, and it is the
date-stamped *filenames* inside it that distinguish two runs. Without the date
in the id, the same numbers on a later day passed the "do not overwrite"
check on disk and then collided here — and were never mirrored at all.

Every database write is wrapped in a `DataError` naming the database and the
run, so a failure at teardown says which session was not mirrored rather than
surfacing as `UNIQUE constraint failed: runs.run_id`.

## Configuration and size

```yaml
database:
  enabled: true               # false turns the mirror off; the files remain the record
  artifact_max_bytes: 10000000
```

The `artifacts` table records **every** file a run produced with its path,
size and sha256 — that is what keeps it identifiable and checkable from the
database alone. It stores the *contents* only for files under
`artifact_max_bytes`; a larger one (an EyeLink EDF is tens of megabytes) keeps
its bytes where they already are, in the run directory, and a log line says
so. Without the cap a season's recording becomes a database nobody can move.

`FrameInputBuffer` holds the frame-level input trace in memory for the whole
session and flushes it transactionally at teardown — roughly one small record
per displayed frame, so about 216 000 records in an hour at 60 Hz. That is
tens of megabytes of Python objects, comfortable on any rig machine, but it
is worth knowing before running a session of several hours.

## Query one displayed frame

```python
from alhazen import ExperimentDatabase

db = ExperimentDatabase("data/experiment.sqlite3")
snapshot = db.frame_snapshot(
    subject="A",
    session=3,
    run=1,
    trial_index=12,
    frame_index=45,
)

print(snapshot["t_session"])
print(snapshot["gaze_x_centered_px"], snapshot["gaze_y_centered_px"])
print(snapshot["keys"])
print(snapshot["devices"])
```

`frame_index` is zero-based within a trial. Gaze is in centered display pixels
(origin at screen center, positive y upward), and gaze and responses are the
exact input snapshot used by the task on that displayed frame. Each entry in
`devices` is the nearest aligned sample for one device/stream/channel.

## Add native device samples

EyeLink and SpikeGLX own their native recordings and clocks. After their
clock has been aligned to the session clock, insert scalar channel samples:

```python
from alhazen import DeviceSample, ExperimentDatabase

db = ExperimentDatabase("data/experiment.sqlite3")
db.ingest_device_samples(
    "sub-A/ses-003/run-01/task-saccade-to-target",
    device="spikeglx",
    stream="nidq",
    sample_rate_hz=25_000,
    samples=[
        DeviceSample(
            channel="imec0.ap:17",
            sample_index=123456,
            t_device=4.93824,
            t_session=81234.5123,
            value=-127,
        )
    ],
)
```

`frames.interval_s` is the FrameMonitor's own interval, the same number
`frames.csv` records — not a delta recomputed between rows, which disagreed
with the CSV on the first frame of every trial.

Both clocks are retained. `t_device` preserves the acquisition record;
`t_session` enables indexed joins to frames and behavioral events. Samples
without an alignment may be inserted with only `t_device`, but cannot appear
in a frame snapshot until an aligned session time is supplied.

Continuous ephys should use compressed dense chunks rather than billions of
scalar SQL rows:

```python
data = spikeglx.memmap_bin(bin_path, meta)  # samples × channels
db.ingest_dense_stream(
    run_id,
    device="spikeglx",
    stream="imec0.ap",
    values=data,
    channels=[str(i) for i in range(data.shape[1])],
    sample_rate_hz=rate,
    t_device_start=0.0,
    t_session_start=float(fit.to_behavior(0.0)),
    session_seconds_per_sample=1.0 / (rate * fit.scale),
)
```

Dense arrays retain their native dtype, are compressed in independently
indexed chunks, and are decoded one relevant chunk at a time by
`frame_snapshot`. This keeps a multi-hour multichannel recording practical
without sacrificing per-channel frame queries.

Raw ephys is not available to alhazen during acquisition: SpikeGLX writes it
on its own host. The run initially stores the recording pointer. Native
samples enter the database after the recording exists and TTL alignment has
been fitted; the database never invents an alignment or silently equates two
machines' clocks.
