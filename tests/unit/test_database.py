"""The per-experiment SQLite mirror: identity, failure modes, and scale.

The run files are the reproducibility record; this is their queryable copy.
Everything here is about the ways a copy can disagree with, or fail to be
made from, the thing it copies.
"""

from __future__ import annotations

import sqlite3

import pytest

from alhazen.config.models import DatabaseConfig
from alhazen.display.frames import FrameRecord
from alhazen.errors import DataError
from alhazen.session.database import (
    DATABASE_FILENAME,
    SCHEMA_VERSION,
    ExperimentDatabase,
    FrameInputBuffer,
)
from support import FRAME_S, SessionHarness, make_session_config


def write_run(tmp_path, *, date="20260826", run=1, session=1, status="complete", **kwargs):
    """Mirror one run into a database, through the same call the runner makes."""
    from alhazen.data.paths import SessionPaths

    cfg = make_session_config(tmp_path)
    cfg = cfg.model_copy(
        update={"info": cfg.info.model_copy(update={"session": session, "run": run})}
    )
    paths = SessionPaths.create(tmp_path, cfg.info.subject, session, run, cfg.info.task_name, date)
    paths.snapshot_path.write_text("config: {}\nprovenance: {}\n")
    database = ExperimentDatabase(tmp_path / DATABASE_FILENAME, **kwargs)
    return database, database.write_run(
        cfg,
        paths,
        trials=[{"trial_index": 1, "attempt": 1, "outcome": "COMPLETED", "success": True}],
        events=[{"trial_index": 1, "event": "TRIAL_START", "t": 0.0, "payload_json": "{}"}],
        frames=[],
        frame_inputs=[],
        status=status,
    )


class TestSchemaCompatibility:
    """`CREATE TABLE IF NOT EXISTS` is a no-op against a file written by an
    older schema, so an outdated mirror used to open cleanly and then fail at
    the first INSERT — "table runs has no column named date", naming neither
    the file nor the fix, at teardown, after a real session had run."""

    def stale(self, tmp_path, version=1):
        """A database with the shape alhazen used before `runs.date` existed."""
        path = tmp_path / DATABASE_FILENAME
        with sqlite3.connect(path) as db:
            db.executescript(
                """
                CREATE TABLE schema_info (version INTEGER NOT NULL);
                CREATE TABLE subjects (subject TEXT PRIMARY KEY);
                CREATE TABLE runs (
                  run_id TEXT PRIMARY KEY,
                  subject TEXT NOT NULL,
                  session INTEGER NOT NULL,
                  run INTEGER NOT NULL,
                  task TEXT NOT NULL,
                  run_dir TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL,
                  config_json TEXT NOT NULL,
                  provenance_json TEXT NOT NULL
                );
                """
            )
            db.execute("INSERT INTO schema_info(version) VALUES (?)", (version,))
        return path

    def test_an_older_database_is_refused_with_the_fix_in_the_message(self, tmp_path):
        path = self.stale(tmp_path)

        with pytest.raises(DataError) as error:
            ExperimentDatabase(path).connect()

        message = str(error.value)
        assert str(path) in message
        assert "schema version 1" in message and f"version {SCHEMA_VERSION}" in message
        # The experimenter has to know their data is safe before they will act.
        assert "mirror" in message and "run directories" in message

    def test_it_refuses_before_writing_anything(self, tmp_path):
        path = self.stale(tmp_path)

        with pytest.raises(DataError):
            ExperimentDatabase(path).connect()

        with sqlite3.connect(path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        assert "trials" not in tables
        assert db.execute("SELECT version FROM schema_info").fetchone()[0] == 1

    def test_a_shape_change_is_caught_even_without_a_version_bump(self, tmp_path):
        # A schema edit shipped without bumping SCHEMA_VERSION would otherwise
        # reproduce the same failure, on a rig rather than in this suite.
        path = self.stale(tmp_path, version=SCHEMA_VERSION)

        with pytest.raises(DataError, match="a different shape for .*runs"):
            ExperimentDatabase(path).connect()

    def test_a_fresh_database_is_stamped_with_the_current_version(self, tmp_path):
        database = ExperimentDatabase(tmp_path / DATABASE_FILENAME)
        with database.connect() as db:
            assert db.execute("SELECT version FROM schema_info").fetchone()[0] == SCHEMA_VERSION
        # And reopening it is not a fight with its own guard.
        database.connect().close()


class TestRunIdentity:
    """`SessionPaths.create` refuses to overwrite the *date-stamped* trials
    file, so the same sub/ses/run on a later day is a legitimate new run. The
    database's run_id carried no date, so that run passed the file check and
    then hit a raw `sqlite3.IntegrityError` at teardown — and was never
    mirrored at all."""

    def test_the_same_numbers_on_two_dates_are_two_runs(self, tmp_path):
        _db, first = write_run(tmp_path, date="20260826")
        database, second = write_run(tmp_path, date="20260827")

        assert first != second
        with database.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2

    def test_the_run_id_carries_the_date(self, tmp_path):
        _db, run_id = write_run(tmp_path, date="20260826")
        assert "20260826" in run_id

    def test_a_genuine_duplicate_raises_a_typed_error(self, tmp_path):
        write_run(tmp_path, date="20260826")

        with pytest.raises(DataError) as excinfo:
            # Same subject, session, run AND date: the same run twice.
            database, _ = write_run(tmp_path, date="20260826")

        message = str(excinfo.value)
        assert DATABASE_FILENAME in message
        assert "20260826" in message  # the run_id it collided on

    def test_a_write_failure_names_the_database(self, tmp_path, monkeypatch):
        database, _ = write_run(tmp_path)
        monkeypatch.setattr(
            ExperimentDatabase,
            "connect",
            lambda self: (_ for _ in ()).throw(sqlite3.OperationalError("disk is full")),
        )

        with pytest.raises(DataError, match=DATABASE_FILENAME):
            write_run(tmp_path, run=2)


class TestFrameIntervals:
    """`frames.interval_s` in the mirror must be the same number as in
    frames.csv. It was recomputed as a delta between consecutive frame-input
    rows, which disagrees on the first frame of every trial — where the CSV
    records the monitor's own interval and the delta has no predecessor."""

    def test_the_timing_rows_own_interval_is_used(self, tmp_path):
        from alhazen.data.paths import SessionPaths
        from alhazen.session.database import FrameInputRecord

        cfg = make_session_config(tmp_path)
        paths = SessionPaths.create(tmp_path, "t01", 1, 1, "test-task", "20260826")
        paths.snapshot_path.write_text("config: {}\nprovenance: {}\n")
        database = ExperimentDatabase(tmp_path / DATABASE_FILENAME)

        frames = [
            FrameRecord(trial_index=1, t=0.0, interval_s=0.0167, dropped=False),
            FrameRecord(trial_index=1, t=0.02, interval_s=0.0200, dropped=True),
        ]
        inputs = [
            FrameInputRecord(1, 0, 0.0, None, None, (), 0.0),
            FrameInputRecord(1, 1, 0.02, None, None, (), 0.0),
        ]
        run_id = database.write_run(
            cfg,
            paths,
            trials=[],
            events=[],
            frames=frames,
            frame_inputs=inputs,
            status="complete",
        )

        with database.connect() as db:
            rows = db.execute(
                "SELECT frame_index, interval_s, dropped FROM frames WHERE run_id = ? "
                "ORDER BY frame_index",
                (run_id,),
            ).fetchall()

        assert rows[0][1] == pytest.approx(0.0167)  # not None, and not a delta
        assert rows[1][1] == pytest.approx(0.0200)
        assert rows[1][2] == 1


class TestArtifactSizePolicy:
    """An EDF is tens of megabytes. Copying it into SQLite alongside the file
    it already sits next to doubles a run's footprint silently."""

    def run_with_artifact(self, tmp_path, size: int, **kwargs):
        from alhazen.data.paths import SessionPaths

        cfg = make_session_config(tmp_path)
        paths = SessionPaths.create(tmp_path, "t01", 1, 1, "test-task", "20260826")
        paths.snapshot_path.write_text("config: {}\nprovenance: {}\n")
        (paths.run_dir / "big.edf").write_bytes(b"\x00" * size)
        database = ExperimentDatabase(tmp_path / DATABASE_FILENAME, **kwargs)
        run_id = database.write_run(
            cfg, paths, trials=[], events=[], frames=[], frame_inputs=[], status="complete"
        )
        return database, run_id

    def artifact(self, database, run_id, name):
        with database.connect() as db:
            return db.execute(
                "SELECT path, bytes, sha256, content FROM artifacts WHERE run_id = ? AND path = ?",
                (run_id, name),
            ).fetchone()

    def test_a_small_file_is_stored_whole(self, tmp_path):
        database, run_id = self.run_with_artifact(tmp_path, 128)

        _path, size, digest, content = self.artifact(database, run_id, "big.edf")

        assert size == 128 and content is not None and len(digest) == 64

    def test_a_file_over_the_cap_is_recorded_but_not_copied(self, tmp_path):
        database, run_id = self.run_with_artifact(
            tmp_path, 4096, config=DatabaseConfig(artifact_max_bytes=1024)
        )

        path, size, digest, content = self.artifact(database, run_id, "big.edf")

        assert path == "big.edf"
        assert size == 4096  # the size is still on the record
        assert len(digest) == 64  # and so is its hash, so it stays identifiable
        assert content is None  # but the bytes are not duplicated

    def test_it_says_so_in_the_log(self, tmp_path, caplog):
        with caplog.at_level("INFO"):
            self.run_with_artifact(tmp_path, 4096, config=DatabaseConfig(artifact_max_bytes=1024))

        assert any("big.edf" in record.message for record in caplog.records)


class TestFrameInputBuffer:
    def test_it_records_one_entry_per_frame(self):
        from alhazen.core.trial import InputFrame

        buffer = FrameInputBuffer()
        for index in range(3):
            buffer.note(1, index, index * FRAME_S, InputFrame(gaze=(1.0, 2.0), keys=("a",)))

        assert len(buffer.records) == 3
        assert buffer.records[0].gaze_x_centered_px == 1.0
        assert buffer.records[0].keys == ("a",)

    def test_an_unverifiable_gaze_stays_missing(self):
        from alhazen.core.trial import InputFrame

        buffer = FrameInputBuffer()
        buffer.note(1, 0, 0.0, InputFrame(gaze=None))

        record = buffer.records[0]
        assert record.gaze_x_centered_px is None and record.gaze_y_centered_px is None


class TestCancelledSessions:
    def test_cancelling_at_the_instructions_screen_is_not_complete(self, tmp_path):
        """It flowed into normal teardown, so the mirror recorded
        `status="complete"` for a session with zero trials — indistinguishable
        from one that ran and produced nothing."""
        database = ExperimentDatabase(tmp_path / DATABASE_FILENAME)
        harness = SessionHarness(tmp_path, n_trials=2)
        harness.runner._database = database
        harness.runner._instructions = "press space"
        harness.runner._await_start = lambda: False

        harness.runner.run()

        with database.connect() as db:
            (status,) = db.execute("SELECT status FROM runs").fetchone()
        assert status == "cancelled"

    def test_a_session_that_ran_is_complete(self, tmp_path):
        database = ExperimentDatabase(tmp_path / DATABASE_FILENAME)
        harness = SessionHarness(tmp_path, n_trials=2)
        harness.runner._database = database

        harness.runner.run()

        with database.connect() as db:
            (status,) = db.execute("SELECT status FROM runs").fetchone()
        assert status == "complete"
