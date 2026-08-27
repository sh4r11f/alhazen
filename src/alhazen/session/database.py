"""Per-experiment SQLite storage and cross-device frame queries.

The immutable run files remain the reproducibility record.  This database is
their queryable mirror: one database at ``data_root/experiment.sqlite3`` holds
all subjects, sessions and runs for that experiment.  Native device streams
which arrive later (EDF/SpikeGLX) enter the same clock-indexed schema through
``ingest_device_samples`` after alignment.

It lives in ``session/`` rather than ``data/`` because it reads a session's
whole vocabulary — ``SessionConfig``, ``InputFrame``, ``FrameRecord`` — and
those sit above ``data/`` in the layering. ``data/`` is pure disk: names,
paths, hashes, the participants registry, and nothing that knows what a trial
is. Putting the mirror here keeps that true, and keeps the layering contract
pointing one way (see architecture.md's deviations table).
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import sqlite3
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import yaml

from alhazen.config.models import DatabaseConfig, SessionConfig
from alhazen.core.trial import InputFrame
from alhazen.display.frames import FrameRecord
from alhazen.errors import DataError

if TYPE_CHECKING:
    from alhazen.data.paths import SessionPaths

log = logging.getLogger(__name__)

DATABASE_FILENAME = "experiment.sqlite3"
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class FrameInputRecord:
    trial_index: int
    frame_index: int
    t: float
    gaze_x_centered_px: float | None
    gaze_y_centered_px: float | None
    keys: tuple[str, ...]
    wheel: float


class FrameInputBuffer:
    """In-memory frame input trace flushed transactionally at teardown."""

    def __init__(self) -> None:
        self.records: list[FrameInputRecord] = []

    def note(self, trial_index: int, frame_index: int, t: float, inputs: InputFrame) -> None:
        gaze_x, gaze_y = inputs.gaze if inputs.gaze is not None else (None, None)
        self.records.append(
            FrameInputRecord(
                trial_index, frame_index, t, gaze_x, gaze_y, tuple(inputs.keys), inputs.wheel
            )
        )


@dataclass(frozen=True)
class DeviceSample:
    """One scalar sample from a named device stream and channel.

    ``t_device`` preserves the acquisition system's clock. ``t_session`` is
    the aligned alhazen clock and is required for frame-level joins.
    """

    channel: str
    value: float | int | str | None
    sample_index: int | None = None
    t_device: float | None = None
    t_session: float | None = None
    metadata: Mapping[str, Any] | None = None


class ExperimentDatabase:
    def __init__(self, path: Path | str, config: DatabaseConfig | None = None) -> None:
        self.path = Path(path)
        self.config = config or DatabaseConfig()

    @classmethod
    def for_data_root(
        cls, data_root: Path | str, config: DatabaseConfig | None = None
    ) -> ExperimentDatabase:
        return cls(Path(data_root) / DATABASE_FILENAME, config)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        _create_schema(connection, self.path)
        return connection

    def write_run(
        self,
        cfg: SessionConfig,
        paths: SessionPaths,
        *,
        trials: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
        frames: Sequence[FrameRecord],
        frame_inputs: Sequence[FrameInputRecord],
        status: str,
    ) -> str:
        """Atomically mirror one completed or failed run and all its files."""
        run_id = _run_id(cfg, paths)
        try:
            return self._write_run(
                run_id,
                cfg,
                paths,
                trials=trials,
                events=events,
                frames=frames,
                frame_inputs=frame_inputs,
                status=status,
            )
        except sqlite3.IntegrityError as error:
            # Almost always the same run twice. Raw, this reached an
            # experimenter as "UNIQUE constraint failed: runs.run_id", naming
            # neither the database nor the run.
            raise DataError(
                f"{self.path} already holds run {run_id!r}, so this session was not "
                f"mirrored. That run is already recorded; use the next run number."
            ) from error
        except sqlite3.Error as error:
            raise DataError(f"could not mirror run {run_id!r} into {self.path}: {error}") from error

    def _write_run(
        self,
        run_id: str,
        cfg: SessionConfig,
        paths: SessionPaths,
        *,
        trials: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
        frames: Sequence[FrameRecord],
        frame_inputs: Sequence[FrameInputRecord],
        status: str,
    ) -> str:
        snapshot = yaml.safe_load(paths.snapshot_path.read_text()) or {}
        run_dir = str(paths.run_dir.resolve())
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO subjects(subject) VALUES (?)", (cfg.info.subject,))
            db.execute(
                """INSERT INTO runs
                   (run_id, subject, session, run, task, date, run_dir, status,
                    config_json, provenance_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    cfg.info.subject,
                    cfg.info.session,
                    cfg.info.run,
                    cfg.info.task_name,
                    _date_stamp(paths),
                    run_dir,
                    status,
                    _json(snapshot.get("config", {})),
                    _json(snapshot.get("provenance", {})),
                ),
            )
            db.executemany(
                """INSERT INTO trials
                   (run_id, trial_index, attempt, outcome, success, record_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        int(row["trial_index"]),
                        _optional_int(row.get("attempt")),
                        row.get("outcome"),
                        _optional_bool(row.get("success")),
                        _json(row),
                    )
                    for row in trials
                ],
            )
            db.executemany(
                """INSERT INTO events
                   (run_id, trial_index, event, t_session, payload_json)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        int(row["trial_index"]),
                        str(row["event"]),
                        float(row["t"]),
                        str(row.get("payload_json") or "{}"),
                    )
                    for row in events
                ],
            )
            timing_by_key = {(frame.trial_index, round(frame.t, 9)): frame for frame in frames}
            previous_t: dict[int, float] = {}
            frame_rows = []
            for frame in frame_inputs:
                timing = timing_by_key.get((frame.trial_index, round(frame.t, 9)))
                prior = previous_t.get(frame.trial_index)
                previous_t[frame.trial_index] = frame.t
                # The FrameMonitor's own interval where the timing row matched.
                # Recomputing it as a delta between consecutive input rows
                # disagrees with frames.csv on the first frame of every trial,
                # where the monitor records a real interval and the delta has
                # no predecessor to subtract. Two records of one measurement
                # that disagree is worse than one.
                interval: float | None
                if timing is not None:
                    interval = timing.interval_s
                else:
                    interval = frame.t - prior if prior is not None else None
                frame_rows.append(
                    (
                        run_id,
                        frame.trial_index,
                        frame.frame_index,
                        frame.t,
                        interval,
                        int(timing.dropped) if timing is not None else 0,
                    )
                )
            db.executemany(
                """INSERT INTO frames
                   (run_id, trial_index, frame_index, t_session, interval_s, dropped)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                frame_rows,
            )
            db.executemany(
                """INSERT INTO frame_inputs
                   (run_id, trial_index, frame_index, t_session, gaze_x_centered_px,
                    gaze_y_centered_px, keys_json, wheel)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        row.trial_index,
                        row.frame_index,
                        row.t,
                        row.gaze_x_centered_px,
                        row.gaze_y_centered_px,
                        _json(row.keys),
                        row.wheel,
                    )
                    for row in frame_inputs
                ],
            )
            _insert_artifacts(db, run_id, paths.run_dir, self.config.artifact_max_bytes)
            _insert_paradigm(db, run_id, paths.paradigm_path)
            training_path = cfg.rig.data_root / f"sub-{cfg.info.subject}" / "training_state.yaml"
            if training_path.exists():
                db.execute(
                    """INSERT INTO training_states(subject, yaml, updated_run_id)
                       VALUES (?, ?, ?) ON CONFLICT(subject) DO UPDATE SET
                       yaml=excluded.yaml, updated_run_id=excluded.updated_run_id""",
                    (cfg.info.subject, training_path.read_text(), run_id),
                )
        return run_id

    def ingest_device_samples(
        self,
        run_id: str,
        device: str,
        stream: str,
        samples: Iterable[DeviceSample],
        *,
        sample_rate_hz: float | None = None,
    ) -> int:
        """Add an aligned native device stream in one transaction."""
        rows = list(samples)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise DataError(f"database has no run {run_id!r}")
            db.execute(
                """INSERT INTO device_streams
                   (run_id, device, stream, sample_rate_hz, storage_kind)
                   VALUES (?, ?, ?, ?, 'sparse')
                   ON CONFLICT(run_id, device, stream) DO UPDATE SET
                   sample_rate_hz=excluded.sample_rate_hz,
                   storage_kind=excluded.storage_kind""",
                (run_id, device, stream, sample_rate_hz),
            )
            db.executemany(
                """INSERT INTO device_samples
                   (run_id, device, stream, channel, sample_index, t_device,
                    t_session, value_real, value_text, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        device,
                        stream,
                        sample.channel,
                        sample.sample_index,
                        sample.t_device,
                        sample.t_session,
                        float(sample.value) if isinstance(sample.value, (int, float)) else None,
                        sample.value if isinstance(sample.value, str) else None,
                        _json(sample.metadata or {}),
                    )
                    for sample in rows
                ],
            )
        return len(rows)

    def ingest_dense_stream(
        self,
        run_id: str,
        device: str,
        stream: str,
        values: np.ndarray,
        *,
        channels: Sequence[str],
        sample_rate_hz: float,
        t_device_start: float,
        t_session_start: float,
        session_seconds_per_sample: float | None = None,
        chunk_samples: int = 100_000,
    ) -> int:
        """Store a continuous multichannel array as compressed indexed chunks.

        ``values`` is shaped ``(samples, channels)``. The session-clock step
        normally comes from a fitted clock transform; it defaults to the
        reciprocal sample rate only when both clocks share a rate.
        """
        array = np.asarray(values)
        if array.ndim != 2 or array.shape[1] != len(channels):
            raise DataError(
                f"dense stream shape {array.shape} does not match {len(channels)} channels"
            )
        if sample_rate_hz <= 0 or chunk_samples <= 0:
            raise DataError("sample_rate_hz and chunk_samples must be positive")
        step = session_seconds_per_sample or 1.0 / sample_rate_hz
        if step <= 0:
            raise DataError("session_seconds_per_sample must be positive")
        contiguous = np.ascontiguousarray(array)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise DataError(f"database has no run {run_id!r}")
            db.execute(
                """INSERT INTO device_streams
                   (run_id, device, stream, sample_rate_hz, storage_kind,
                    channels_json, dtype, t_device_start, t_session_start,
                    session_seconds_per_sample)
                   VALUES (?, ?, ?, ?, 'dense', ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, device, stream) DO UPDATE SET
                     sample_rate_hz=excluded.sample_rate_hz,
                     storage_kind='dense', channels_json=excluded.channels_json,
                     dtype=excluded.dtype, t_device_start=excluded.t_device_start,
                     t_session_start=excluded.t_session_start,
                     session_seconds_per_sample=excluded.session_seconds_per_sample""",
                (
                    run_id,
                    device,
                    stream,
                    sample_rate_hz,
                    _json(channels),
                    contiguous.dtype.str,
                    t_device_start,
                    t_session_start,
                    step,
                ),
            )
            db.execute(
                "DELETE FROM device_chunks WHERE run_id=? AND device=? AND stream=?",
                (run_id, device, stream),
            )
            chunks = []
            for chunk_index, start in enumerate(range(0, len(contiguous), chunk_samples)):
                chunk = contiguous[start : start + chunk_samples]
                chunks.append(
                    (
                        run_id,
                        device,
                        stream,
                        chunk_index,
                        start,
                        len(chunk),
                        zlib.compress(chunk.tobytes(order="C")),
                    )
                )
            db.executemany(
                """INSERT INTO device_chunks
                   (run_id, device, stream, chunk_index, first_sample, n_samples, data_zlib)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                chunks,
            )
        return int(array.shape[0])

    def find_run(
        self, subject: str, session: int, *, run: int | None = None, task: str | None = None
    ) -> dict[str, Any]:
        clauses = ["subject = ?", "session = ?"]
        params: list[Any] = [subject, session]
        if run is not None:
            clauses.append("run = ?")
            params.append(run)
        if task is not None:
            clauses.append("task = ?")
            params.append(task)
        sql = f"SELECT * FROM runs WHERE {' AND '.join(clauses)} ORDER BY run DESC"  # noqa: S608
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        if not rows:
            raise DataError(f"no database run for subject={subject!r}, session={session}")
        if len(rows) > 1 and run is None and task is None:
            raise DataError("more than one run matches; specify run= or task=")
        return dict(rows[0])

    def frame_snapshot(
        self,
        *,
        subject: str,
        session: int,
        trial_index: int,
        frame_index: int,
        run: int | None = None,
        task: str | None = None,
    ) -> dict[str, Any]:
        """Frame timing, behavioral input, and nearest aligned device channels."""
        run_row = self.find_run(subject, session, run=run, task=task)
        run_id = run_row["run_id"]
        with self.connect() as db:
            frame = db.execute(
                """SELECT f.*, i.gaze_x_centered_px, i.gaze_y_centered_px,
                          i.keys_json, i.wheel
                   FROM frames f LEFT JOIN frame_inputs i USING
                     (run_id, trial_index, frame_index)
                   WHERE f.run_id=? AND f.trial_index=? AND f.frame_index=?""",
                (run_id, trial_index, frame_index),
            ).fetchone()
            if frame is None:
                raise DataError(f"run {run_id} has no trial {trial_index}, frame {frame_index}")
            device_rows = db.execute(
                """SELECT s.* FROM device_samples s
                   JOIN (
                     SELECT device, stream, channel, MIN(ABS(t_session - ?)) AS delta
                     FROM device_samples WHERE run_id=? AND t_session IS NOT NULL
                     GROUP BY device, stream, channel
                   ) nearest
                   ON nearest.device=s.device AND nearest.stream=s.stream
                     AND nearest.channel=s.channel
                     AND ABS(s.t_session - ?)=nearest.delta
                   WHERE s.run_id=?""",
                (frame["t_session"], run_id, frame["t_session"], run_id),
            ).fetchall()
            dense_streams = db.execute(
                """SELECT * FROM device_streams
                   WHERE run_id=? AND storage_kind='dense'""",
                (run_id,),
            ).fetchall()
        result = dict(frame)
        result["keys"] = json.loads(result.pop("keys_json") or "[]")
        result["devices"] = [dict(row) for row in device_rows]
        for stream_row in dense_streams:
            result["devices"].extend(self._dense_values_at(run_id, stream_row, result["t_session"]))
        return result

    def _dense_values_at(
        self, run_id: str, stream: sqlite3.Row, t_session: float
    ) -> list[dict[str, Any]]:
        step = float(stream["session_seconds_per_sample"])
        sample_index = round((t_session - float(stream["t_session_start"])) / step)
        if sample_index < 0:
            return []
        with self.connect() as db:
            chunk = db.execute(
                """SELECT * FROM device_chunks
                   WHERE run_id=? AND device=? AND stream=?
                     AND first_sample<=? AND first_sample+n_samples>?
                   ORDER BY first_sample DESC LIMIT 1""",
                (run_id, stream["device"], stream["stream"], sample_index, sample_index),
            ).fetchone()
        if chunk is None:
            return []
        channels = json.loads(stream["channels_json"])
        dtype = np.dtype(stream["dtype"])
        values = np.frombuffer(zlib.decompress(chunk["data_zlib"]), dtype=dtype).reshape(
            int(chunk["n_samples"]), len(channels)
        )
        offset = sample_index - int(chunk["first_sample"])
        t_device = float(stream["t_device_start"]) + sample_index / float(stream["sample_rate_hz"])
        return [
            {
                "run_id": run_id,
                "device": stream["device"],
                "stream": stream["stream"],
                "channel": channel,
                "sample_index": sample_index,
                "t_device": t_device,
                "t_session": float(stream["t_session_start"]) + sample_index * step,
                "value_real": float(values[offset, channel_index]),
                "value_text": None,
                "metadata_json": "{}",
            }
            for channel_index, channel in enumerate(channels)
        ]


def _run_id(cfg: SessionConfig, paths: SessionPaths) -> str:
    """This run's identity in the database.

    The DATE is part of it, because it is part of what makes a run unique on
    disk: `SessionPaths.create` refuses to overwrite the date-stamped trials
    file, so the same subject/session/run on a later day is a legitimate new
    run. Without the date that run passed the file check and then collided in
    the database — and was never mirrored at all.
    """
    info = cfg.info
    return (
        f"sub-{info.subject}/ses-{info.session:03d}/run-{info.run:02d}/"
        f"task-{info.task_name}/{_date_stamp(paths)}"
    )


def _date_stamp(paths: SessionPaths) -> str:
    """The YYYYMMDD the run directory's filenames carry."""
    return paths.base.rsplit("_", 1)[-1]


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        return int(value.lower() == "true")
    return int(bool(value))


def _insert_artifacts(db: sqlite3.Connection, run_id: str, run_dir: Path, max_bytes: int) -> None:
    """Record every file the run produced; copy in the ones worth copying.

    Path, size and sha256 go in whatever the size — that is what keeps a file
    identifiable and checkable from the database alone. The CONTENT is copied
    only under the cap: an EyeLink EDF is tens of megabytes, it already sits
    in the run directory, and duplicating it would silently double a season's
    footprint. A skipped file says so in the log rather than being quietly
    different from its neighbours.
    """
    rows = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        relative = str(path.relative_to(run_dir))
        content: bytes | None = None
        if size <= max_bytes:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
        else:
            digest = _sha256_of(path)
            log.info(
                "%s is %d bytes, over the database's artifact_max_bytes of %d — recording "
                "its path, size and hash, leaving its contents in the run directory",
                relative,
                size,
                max_bytes,
            )
        rows.append(
            (
                run_id,
                relative,
                mimetypes.guess_type(path.name)[0],
                size,
                digest,
                content,
            )
        )
    db.executemany(
        "INSERT INTO artifacts (run_id, path, media_type, bytes, sha256, content) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _insert_paradigm(db: sqlite3.Connection, run_id: str, path: Path) -> None:
    if not path.exists():
        return
    import csv

    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    db.executemany(
        "INSERT INTO paradigm_rows(run_id, row_index, record_json) VALUES (?, ?, ?)",
        [(run_id, index, _json(row)) for index, row in enumerate(rows)],
    )


# The whole schema, as one script. A module constant rather than a literal
# inside the creating function because it is used twice: once to build the
# real database, and once to build a throwaway in-memory one whose shape an
# existing file is checked against.
# Bootstrapped on its own, before the rest, so the version can be read and
# judged before any other table is touched. Shared with _SCHEMA_SQL rather
# than retyped: the shape check below compares stored SQL text, and two
# spellings of the same table would read as a schema mismatch.
_SCHEMA_INFO_SQL = """
        CREATE TABLE IF NOT EXISTS schema_info (
          version INTEGER NOT NULL
        );
"""

_SCHEMA_SQL = (
    _SCHEMA_INFO_SQL
    + """
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY,
          subject TEXT NOT NULL,
          session INTEGER NOT NULL,
          run INTEGER NOT NULL,
          task TEXT NOT NULL,
          -- YYYYMMDD, part of a run's identity because it is part of its
          -- identity on disk: the run directory is shared, and it is the
          -- date-stamped FILENAMES that distinguish two runs in it.
          date TEXT NOT NULL,
          -- Not unique: the same subject/session/run on two days writes two
          -- runs into one directory, distinguished by their date stamps.
          run_dir TEXT NOT NULL,
          status TEXT NOT NULL,
          config_json TEXT NOT NULL,
          provenance_json TEXT NOT NULL,
          UNIQUE(subject, session, run, task, date)
        );
        CREATE INDEX IF NOT EXISTS runs_subject_session ON runs(subject, session);
        CREATE TABLE IF NOT EXISTS subjects (
          subject TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS training_states (
          subject TEXT PRIMARY KEY REFERENCES subjects(subject) ON DELETE CASCADE,
          yaml TEXT NOT NULL,
          updated_run_id TEXT NOT NULL REFERENCES runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS trials (
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          trial_index INTEGER NOT NULL,
          attempt INTEGER,
          outcome TEXT,
          success INTEGER,
          record_json TEXT NOT NULL,
          PRIMARY KEY(run_id, trial_index)
        );
        CREATE TABLE IF NOT EXISTS events (
          event_id INTEGER PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          trial_index INTEGER NOT NULL,
          event TEXT NOT NULL,
          t_session REAL NOT NULL,
          payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_time ON events(run_id, t_session);
        CREATE TABLE IF NOT EXISTS frames (
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          trial_index INTEGER NOT NULL,
          frame_index INTEGER NOT NULL,
          t_session REAL NOT NULL,
          interval_s REAL,
          dropped INTEGER NOT NULL,
          PRIMARY KEY(run_id, trial_index, frame_index)
        );
        CREATE INDEX IF NOT EXISTS frames_time ON frames(run_id, t_session);
        CREATE TABLE IF NOT EXISTS frame_inputs (
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          trial_index INTEGER NOT NULL,
          frame_index INTEGER NOT NULL,
          t_session REAL NOT NULL,
          gaze_x_centered_px REAL,
          gaze_y_centered_px REAL,
          keys_json TEXT NOT NULL,
          wheel REAL NOT NULL,
          PRIMARY KEY(run_id, trial_index, frame_index)
        );
        CREATE TABLE IF NOT EXISTS device_streams (
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          device TEXT NOT NULL,
          stream TEXT NOT NULL,
          sample_rate_hz REAL,
          storage_kind TEXT NOT NULL DEFAULT 'sparse',
          channels_json TEXT,
          dtype TEXT,
          t_device_start REAL,
          t_session_start REAL,
          session_seconds_per_sample REAL,
          PRIMARY KEY(run_id, device, stream)
        );
        CREATE TABLE IF NOT EXISTS device_samples (
          sample_id INTEGER PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          device TEXT NOT NULL,
          stream TEXT NOT NULL,
          channel TEXT NOT NULL,
          sample_index INTEGER,
          t_device REAL,
          t_session REAL,
          value_real REAL,
          value_text TEXT,
          metadata_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS device_samples_time
          ON device_samples(run_id, device, stream, channel, t_session);
        CREATE TABLE IF NOT EXISTS device_chunks (
          run_id TEXT NOT NULL,
          device TEXT NOT NULL,
          stream TEXT NOT NULL,
          chunk_index INTEGER NOT NULL,
          first_sample INTEGER NOT NULL,
          n_samples INTEGER NOT NULL,
          data_zlib BLOB NOT NULL,
          PRIMARY KEY(run_id, device, stream, chunk_index),
          FOREIGN KEY(run_id, device, stream)
            REFERENCES device_streams(run_id, device, stream) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS artifacts (
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          path TEXT NOT NULL,
          media_type TEXT,
          bytes INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          -- NULL for a file over the database's artifact_max_bytes: recorded
          -- and identifiable, but not duplicated out of the run directory.
          content BLOB,
          PRIMARY KEY(run_id, path)
        );
        CREATE TABLE IF NOT EXISTS paradigm_rows (
          run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
          row_index INTEGER NOT NULL,
          record_json TEXT NOT NULL,
          PRIMARY KEY(run_id, row_index)
        );
"""
)


def _create_schema(db: sqlite3.Connection, path: Path | None = None) -> None:
    """Create the schema, or refuse to touch a database that is not ours.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op against a file written by an
    older schema, so without this an outdated mirror opened cleanly and then
    failed at the first INSERT with a raw sqlite message — "table runs has no
    column named date" — that named neither the file nor the fix.
    """
    db.executescript(_SCHEMA_INFO_SQL)
    row = db.execute("SELECT version FROM schema_info").fetchone()
    if row is not None and row[0] != SCHEMA_VERSION:
        raise DataError(_incompatible_message(path, f"schema version {row[0]}"))
    db.executescript(_SCHEMA_SQL)
    if row is None:
        db.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
    _require_expected_shape(db, path)


def _sqlite_objects(db: sqlite3.Connection) -> dict[str, str]:
    """Every table and index this database defines, as normalised SQL."""
    return {
        name: " ".join(sql.split())
        for name, sql in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') "
            "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        )
    }


@lru_cache(maxsize=1)
def _expected_objects() -> dict[str, str]:
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(_SCHEMA_SQL)
        return _sqlite_objects(scratch)
    finally:
        scratch.close()


def _require_expected_shape(db: sqlite3.Connection, path: Path | None) -> None:
    """The version number is a promise; this checks that it was kept.

    A schema change shipped without bumping ``SCHEMA_VERSION`` would otherwise
    reproduce exactly the failure this guard exists to prevent, and would do it
    on someone's rig rather than in the test suite.
    """
    actual = _sqlite_objects(db)
    expected = _expected_objects()
    differing = sorted(
        name for name in set(expected) | set(actual) if expected.get(name) != actual.get(name)
    )
    if differing:
        shape = f"a different shape for {', '.join(differing)}"
        raise DataError(_incompatible_message(path, shape))


def _incompatible_message(path: Path | None, found: str) -> str:
    name = str(path) if path is not None else "this experiment database"
    return (
        f"{name} was written by an incompatible version of alhazen's database schema "
        f"(found {found}; this alhazen writes schema version {SCHEMA_VERSION}). It was NOT "
        f"written to, and nothing was lost: the database is a queryable mirror, and the "
        f"record is the run directories beside it — as is each subject's "
        f"training_state.yaml. Move or delete the file and it is rebuilt from the next "
        f"session onward."
    )
