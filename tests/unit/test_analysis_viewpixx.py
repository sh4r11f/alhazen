"""The ViewPixx (TRACKPixx3) reader: the device's own header, the clock fit,
and what a blink becomes.

Two kinds of fixture, on purpose. ``RunBuilder`` writes synthetic recordings
with the DEVICE's header (``REAL_HEADER``, verbatim) and injected clock
parameters, so a test can say what should come out. ``tests/fixtures/
trackpixx3/`` holds the first forty rows and every message of a real
recording made on the rig — the only TRACKPixx3 data that exists here — so
the reader is also held to a file the device actually wrote, whitespace,
typo and all. The reader used to pass every synthetic test and fail on the
real file, because the fixtures wrote the names the reader wanted.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from alhazen.analysis.io import viewpixx
from alhazen.analysis.io.viewpixx import (
    DEFAULT_COLUMNS,
    REAL_HEADER,
    BinocularRecording,
    ClockFit,
    event_times,
    fit_clock,
    read_run,
    read_run_binocular,
)
from alhazen.config.models import MonitorConfig
from alhazen.display.screen import Screen
from alhazen.errors import DataError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "trackpixx3"

SAMPLE_HZ = 2000.0
# The geometry the snapshot below implies, through the framework's own
# conversion, so the test and the reader cannot disagree about a degree.
PX_PER_DEG = Screen.from_monitor(
    MonitorConfig(
        width_px=1920, height_px=1080, width_cm=52.1, distance_cm=57.0, refresh_rate_hz=120.0
    )
).px_per_deg
# The device clock runs slightly fast and started earlier than the session's.
DEVICE_RATE_ERROR = 1.0 + 2e-5
DEVICE_OFFSET_S = 7000.0


def write_snapshot(run_dir: Path) -> None:
    (run_dir / "config_snapshot.yaml").write_text(
        yaml.safe_dump(
            {
                "config": {
                    "rig": {
                        "monitor": {
                            "width_px": 1920,
                            "height_px": 1080,
                            "width_cm": 52.1,
                            "distance_cm": 57.0,
                            "refresh_rate_hz": 120.0,
                        }
                    }
                }
            }
        )
    )


class RunBuilder:
    """A synthetic ViewPixx run: trials of samples at 2000 Hz, with the
    messages the backend would have written, in the device's own header."""

    def __init__(self, run_dir: Path, eye: str = "left") -> None:
        self.run_dir = run_dir
        self.eye = eye
        self.samples: list[dict[str, float]] = []
        self.messages: list[tuple[float, float, str]] = []
        self.t = 1.0  # session seconds
        self.trial = 0

    def device_time(self, t_session: float) -> float:
        return t_session / DEVICE_RATE_ERROR + DEVICE_OFFSET_S

    def mark(self, text: str) -> None:
        self.messages.append((self.device_time(self.t), self.t, text))

    def trial_of(
        self,
        duration_s: float = 0.5,
        gaze_px: tuple[float, float] = (0.0, 0.0),
        blink_s: tuple[float, float] | None = None,
        lost_s: tuple[float, float] | None = None,
        end_mark: bool = True,
        eye_lost_s: dict[str, tuple[float, float]] | None = None,
        eye_offset_px: dict[str, float] | None = None,
        mark_eye_used: bool = True,
    ) -> None:
        """One trial of samples.

        ``lost_s`` loses both eyes together, which is a blink. ``eye_lost_s``
        loses them separately — ``{"left": (0.1, 0.2)}`` — which is the case
        a binocular reader has to represent and a monocular one cannot see.
        ``eye_offset_px`` shifts one eye's x, so the two eyes differ by a
        known amount and vergence is a number a test can predict.
        """
        self.trial += 1
        self.mark(f"TRIAL {self.trial} attempt 1")
        if mark_eye_used:
            self.mark(f"EYE_USED {self.eye}")
        self.mark("trial_start")
        start = self.t
        n = int(duration_s * SAMPLE_HZ)
        eye_lost_s = eye_lost_s or {}
        offsets = {"left": 0.0, "right": 10.0, **(eye_offset_px or {})}
        for index in range(n):
            t = start + index / SAMPLE_HZ
            since = t - start
            blink = 0.0
            if blink_s is not None and blink_s[0] <= since < blink_s[1]:
                blink = 1.0  # the device's flag, position still a number

            def position(eye: str, since: float = since) -> tuple[float, float]:
                x, y = gaze_px
                x = x + offsets[eye]
                window = eye_lost_s.get(eye)
                both = lost_s is not None and lost_s[0] <= since < lost_s[1]
                one = window is not None and window[0] <= since < window[1]
                if both or one:
                    return 9000.0, 9000.0  # the device's lost sentinel
                return x, y

            left_x, left_y = position("left")
            right_x, right_y = position("right")
            self.samples.append(
                {
                    "Timestamp": self.device_time(t),
                    "\tLeft Screen X": left_x,
                    " Left Screen Y": left_y,
                    " Right Screen X": right_x,
                    " Right Screen Y": right_y,
                    " Left Blink": blink,
                    " Right Blink": blink,
                    " Left Pupil Diameter": 3.0,
                    " Right Pupil Diameter": 4.0,
                }
            )
            if index == n // 4:
                self.t = t
                self.mark("stim_on")
        self.t = start + duration_s
        if end_mark:
            self.mark("trial_end")
        self.t += 0.3  # inter-trial gap

    def write(self, header: tuple[str, ...] = REAL_HEADER) -> Path:
        base = self.run_dir / "sub-x01_ses-001_run-01_task-demo_20260101"
        frame = pd.DataFrame(self.samples)
        for column in header:
            if column not in frame:
                frame[column] = np.nan
        frame[list(header)].to_csv(f"{base}_gaze.csv", index=False)
        pd.DataFrame(self.messages, columns=["device_time_s", "session_time_s", "message"]).to_csv(
            f"{base}_gaze-messages.csv", index=False
        )
        write_snapshot(self.run_dir)
        return self.run_dir


@pytest.fixture
def simple_run(tmp_path):
    builder = RunBuilder(tmp_path)
    builder.trial_of(gaze_px=(0.0, 0.0))
    builder.trial_of(gaze_px=(PX_PER_DEG * 4.0, -PX_PER_DEG * 2.0))
    builder.trial_of(gaze_px=(0.0, 0.0))
    return builder.write()


# ----------------------------------------------------------------------
# The device's own header
# ----------------------------------------------------------------------


class TestTheRealHeader:
    def test_default_columns_resolve_against_the_captured_header(self):
        frame = pd.DataFrame(columns=list(REAL_HEADER))
        resolved = viewpixx._resolve_columns(frame, DEFAULT_COLUMNS, Path("real_header"))

        assert set(resolved) == set(DEFAULT_COLUMNS)
        # Resolved to the device's own spelling, whitespace included: the
        # header is ", "-separated, so the names carry it.
        assert resolved["device_time_s"] == "Timestamp"
        assert resolved["left_x_px"] == "\tLeft Screen X"
        assert resolved["left_y_px"] == " Left Screen Y"
        assert resolved["right_x_px"] == " Right Screen X"
        assert resolved["right_y_px"] == " Right Screen Y"

    def test_the_constant_matches_the_file_the_device_wrote(self):
        """REAL_HEADER is a claim about the device; the fixture is the evidence."""
        header = pd.read_csv(next(FIXTURES.glob("*_gaze.csv"))).columns.tolist()
        assert tuple(header) == REAL_HEADER
        assert " Right Fixaion" in header  # VPixx's typo, matched as written

    def test_the_real_recording_is_read(self, tmp_path):
        """Forty rows and eleven marks of a recording made with no calibration
        on the device: every position NaN, every blink flag set. It must read
        — as forty rows of gap, on the session clock, at 2000 Hz."""
        for path in FIXTURES.glob("*.csv"):
            shutil.copy(path, tmp_path / path.name)
        write_snapshot(tmp_path)

        recording = read_run(tmp_path)

        assert len(recording.samples) == 40
        assert not recording.samples["tracked"].any()
        assert recording.samples["x_dva"].isna().all()
        assert recording.samples["t_session"].is_monotonic_increasing
        assert recording.sample_rate_hz == pytest.approx(SAMPLE_HZ, rel=1e-3)
        assert recording.eye == "left"
        assert recording.fit.n_marks == 11
        # Two real clocks that ran together: the fit is tighter than a sample.
        assert recording.fit.max_residual_s < 0.5 / SAMPLE_HZ
        spans = recording.trial_spans()
        assert spans["trial_index"].tolist() == [1, 2]
        assert spans["t_end"].tolist() == pytest.approx([3.8785860000061803, 5.994467799959239])


# ----------------------------------------------------------------------
# Reading and clock alignment
# ----------------------------------------------------------------------


class TestReading:
    def test_the_clock_fit_recovers_the_injected_map(self, simple_run):
        recording = read_run(simple_run)
        assert recording.fit.slope == pytest.approx(DEVICE_RATE_ERROR, rel=1e-6)
        assert recording.fit.intercept == pytest.approx(
            -DEVICE_OFFSET_S * DEVICE_RATE_ERROR, abs=1e-3
        )
        assert recording.fit.max_residual_s < 1e-6
        assert recording.eye == "left"
        assert recording.gaze_frame == "centered_y_up"

    def test_positions_come_out_in_degrees_from_the_centre(self, simple_run):
        recording = read_run(simple_run)
        second = recording.trial_spans().iloc[1]
        samples = recording.between(second["t_start"], second["t_end"])
        assert samples["tracked"].all()
        assert samples["x_dva"].median() == pytest.approx(4.0, abs=1e-6)
        assert samples["y_dva"].median() == pytest.approx(-2.0, abs=1e-6)
        assert samples["pupil"].median() == pytest.approx(3.0)

    def test_a_screen_frame_recording_is_converted_to_the_centre(self, tmp_path):
        """A recording calibrated by a tool that handed the device top-left
        targets holds screen px, y down; said so, it lands in the same frame."""
        builder = RunBuilder(tmp_path)
        builder.trial_of(gaze_px=(960.0 + PX_PER_DEG * 4.0, 540.0 + PX_PER_DEG * 2.0))
        run = builder.write()
        recording = read_run(run, gaze_frame="screen_y_down")
        assert recording.samples["x_dva"].median() == pytest.approx(4.0, abs=1e-6)
        assert recording.samples["y_dva"].median() == pytest.approx(-2.0, abs=1e-6)
        with pytest.raises(DataError, match="gaze_frame must be"):
            read_run(run, gaze_frame="upside_down")  # type: ignore[arg-type]

    def test_gaze_that_is_mostly_off_the_panel_is_refused_as_the_wrong_frame(self, tmp_path):
        """Screen-frame samples read as centred sit half a panel away — and
        sit there together, so the cluster stays tight, the clock fit stays
        good, and nothing else here has any reason to complain. That is the
        whole danger, so it is checked rather than documented."""
        builder = RunBuilder(tmp_path)
        builder.trial_of(gaze_px=(1100.0, 700.0))  # a little right of and below centre, screen px
        run = builder.write()

        with pytest.raises(DataError, match="outside the .* panel") as error:
            read_run(run)
        assert "screen_y_down" in str(error.value)

        # Told what the recording is, it reads — and lands where it should.
        recording = read_run(run, gaze_frame="screen_y_down")
        assert recording.samples["x_dva"].median() == pytest.approx(140 / PX_PER_DEG, abs=1e-6)
        # And the check can be waived for gaze that really was off the panel.
        assert read_run(run, check_bounds=False).samples["tracked"].all()

    def test_a_little_gaze_off_the_panel_is_a_warning_not_a_refusal(self, tmp_path, caplog):
        """A subject does look away, and a calibration does extrapolate past
        the edges; neither is a reason to refuse a run."""
        import logging

        builder = RunBuilder(tmp_path)
        for _ in range(9):
            builder.trial_of(duration_s=0.05, gaze_px=(0.0, 0.0))
        builder.trial_of(duration_s=0.05, gaze_px=(1500.0, 0.0))  # off the panel's right edge
        run = builder.write()

        with caplog.at_level(logging.WARNING, logger="alhazen.analysis.io.viewpixx"):
            recording = read_run(run)

        assert len(recording.samples) == 1000  # ten trials of 0.05 s at 2000 Hz
        (warning,) = [r for r in caplog.records if "outside" in r.getMessage()]
        assert "10%" in warning.getMessage()

    def test_a_recording_with_no_tracked_gaze_says_nothing_about_the_frame(self, tmp_path):
        """The real fixture is exactly this: a run made with no calibration on
        the device, every sample NaN. It has no evidence to offer either way."""
        for path in FIXTURES.glob("*.csv"):
            shutil.copy(path, tmp_path / path.name)
        write_snapshot(tmp_path)
        assert not read_run(tmp_path).samples["tracked"].any()

    def test_a_drifting_clock_is_refused_rather_than_averaged_away(self):
        device = np.linspace(0, 100, 11)
        session = device + 0.002 * (device / 100) ** 2  # a curve, not a line
        messages = pd.DataFrame(
            {"device_time_s": device, "session_time_s": session, "message": "x"}
        )
        with pytest.raises(DataError, match="do not fit a straight line"):
            fit_clock(messages, tolerance_s=0.0002)

    def test_two_alignment_marks_are_refused(self):
        messages = pd.DataFrame(
            {"device_time_s": [0.0, 1.0], "session_time_s": [0.0, 1.0], "message": ["a", "b"]}
        )
        with pytest.raises(DataError, match="at least 3 alignment marks"):
            fit_clock(messages, tolerance_s=0.001)

    def test_the_fit_maps_scalars_and_arrays(self):
        fit = ClockFit(slope=2.0, intercept=1.0, max_residual_s=0.0, n_marks=3)
        assert fit.to_session(1.0) == pytest.approx(3.0)
        assert fit.to_session(np.array([0.0, 1.0])).tolist() == [1.0, 3.0]


class TestGaps:
    def test_lost_samples_become_nan_positions_and_are_not_dropped(self, tmp_path):
        """A blink is a gap at a known time. Deleting the rows would let a
        differentiator interpolate across it and invent a saccade."""
        builder = RunBuilder(tmp_path)
        builder.trial_of(lost_s=(0.1, 0.2))
        recording = read_run(builder.write())
        lost = ~recording.samples["tracked"]
        # One sample either way: the interval's edges fall between samples.
        assert lost.sum() == pytest.approx(0.1 * SAMPLE_HZ, abs=1)
        assert recording.samples.loc[lost, "x_dva"].isna().all()
        assert recording.samples.loc[lost, "pupil"].isna().all()
        assert len(recording.samples) == int(0.5 * SAMPLE_HZ)

    def test_the_devices_blink_flag_is_a_gap_too(self, tmp_path):
        """The real file flags blinks in their own column while the position
        column still holds a number; the flag wins."""
        builder = RunBuilder(tmp_path)
        builder.trial_of(blink_s=(0.3, 0.35))
        recording = read_run(builder.write())
        assert (~recording.samples["tracked"]).sum() == int(0.05 * SAMPLE_HZ)

    def test_a_file_without_the_optional_columns_still_reads(self, tmp_path):
        builder = RunBuilder(tmp_path)
        builder.trial_of()
        header = tuple(c for c in REAL_HEADER if "Blink" not in c and "Pupil" not in c)
        recording = read_run(builder.write(header=header))
        assert recording.samples["tracked"].all()
        assert "pupil" not in recording.samples

    def test_average_is_the_mean_where_both_eyes_are_tracked(self, tmp_path):
        builder = RunBuilder(tmp_path, eye="average")
        builder.trial_of(gaze_px=(PX_PER_DEG * 2.0, 0.0), lost_s=(0.0, 0.1))
        recording = read_run(builder.write())
        assert recording.eye == "average"
        tracked = recording.samples[recording.samples["tracked"]]
        # Left at 2°, right 10 px further: the mean of the two.
        assert tracked["x_dva"].median() == pytest.approx(2.0 + 5.0 / PX_PER_DEG, abs=1e-6)
        assert tracked["pupil"].median() == pytest.approx(3.5)
        assert (~recording.samples["tracked"]).sum() == int(0.1 * SAMPLE_HZ)


class TestLoudFailures:
    def test_a_wrong_column_map_says_what_the_file_actually_has(self, simple_run):
        with pytest.raises(DataError, match="does not have the columns") as e:
            read_run(simple_run, columns={"left_x_px": "NoSuchColumn"})
        assert "'NoSuchColumn' (for left_x_px)" in str(e.value)
        assert "Left Screen X" in str(e.value)

    def test_naming_style_is_forgiven_but_the_words_are_not(self, simple_run):
        path = next(simple_run.glob("*_gaze.csv"))
        frame = pd.read_csv(path).rename(
            columns={
                "Timestamp": "TIMESTAMP",
                "\tLeft Screen X": "left_screen_x",
                " Left Screen Y": "LeftScreenY",
                " Right Screen X": "right screen x",
                " Right Screen Y": "Right.Screen.Y",
            }
        )
        frame.to_csv(path, index=False)
        assert read_run(simple_run).samples["tracked"].all()

        frame.rename(columns={"left_screen_x": "LeftEyeX"}).to_csv(path, index=False)
        with pytest.raises(DataError, match="does not have the columns"):
            read_run(simple_run)

    def test_a_missing_snapshot_refuses_to_guess_the_geometry(self, simple_run):
        (simple_run / "config_snapshot.yaml").unlink()
        with pytest.raises(DataError, match="monitor geometry"):
            read_run(simple_run)

    def test_a_run_with_no_eye_mark_needs_to_be_told(self, tmp_path):
        builder = RunBuilder(tmp_path)
        builder.trial_of()
        builder.messages = [m for m in builder.messages if not m[2].startswith("EYE_USED")]
        run = builder.write()
        with pytest.raises(DataError, match="no EYE_USED mark"):
            read_run(run)
        assert read_run(run, eye="right").eye == "right"
        with pytest.raises(DataError, match="eye must be"):
            read_run(run, eye="both")

    def test_missing_files_are_named(self, tmp_path):
        write_snapshot(tmp_path)
        with pytest.raises(DataError, match=r"no file matching '\*_gaze.csv'"):
            read_run(tmp_path)


# ----------------------------------------------------------------------
# Events and trials
# ----------------------------------------------------------------------


class TestEventsAndTrials:
    def test_event_times_assigns_each_event_to_its_own_trial(self, simple_run):
        recording = read_run(simple_run)
        onsets = event_times(recording.messages, "STIM_ON")
        assert onsets["trial_index"].tolist() == [1, 2, 3]
        spans = recording.trial_spans()
        for (_, onset), (_, span) in zip(onsets.iterrows(), spans.iterrows(), strict=True):
            assert span["t_start"] < onset["t_session"] < span["t_end"]

    def test_an_event_a_trial_never_produced_leaves_no_row(self, simple_run):
        recording = read_run(simple_run)
        assert event_times(recording.messages, "never_sent").empty

    def test_a_trial_without_an_end_mark_ends_at_the_next_trial(self, tmp_path):
        builder = RunBuilder(tmp_path)
        builder.trial_of(end_mark=False)
        builder.trial_of(end_mark=False)
        recording = read_run(builder.write())
        spans = recording.trial_spans()
        assert spans["t_end"].iloc[0] == pytest.approx(spans["t_start"].iloc[1])
        # The last one ends where the samples do.
        assert spans["t_end"].iloc[1] == pytest.approx(recording.samples["t_session"].iloc[-1])
        assert spans["status"].tolist() == ["attempt 1", "attempt 1"]


# ----------------------------------------------------------------------
# Both eyes
# ----------------------------------------------------------------------


class TestBinocular:
    """`read_run_binocular` keeps what the monocular reader reduces away.

    For a vergence experiment one eye is not a smaller version of the
    measurement, it is none of it, so this is a separate entry point rather
    than an argument — and everything above the eye selection is the same
    code the monocular tests above already cover.
    """

    def test_it_keeps_both_eyes_in_the_same_units(self, tmp_path):
        builder = RunBuilder(tmp_path)
        # Left at 4 deg, right one degree nearer the nose: 1 deg of vergence.
        builder.trial_of(
            gaze_px=(PX_PER_DEG * 4.0, -PX_PER_DEG * 2.0),
            eye_offset_px={"right": -PX_PER_DEG},
        )
        recording = read_run_binocular(builder.write())

        assert isinstance(recording, BinocularRecording)
        assert recording.gaze_frame == "centered_y_up"
        samples = recording.samples
        assert samples["left_x_dva"].median() == pytest.approx(4.0, abs=1e-6)
        assert samples["right_x_dva"].median() == pytest.approx(3.0, abs=1e-6)
        assert samples["left_y_dva"].median() == pytest.approx(-2.0, abs=1e-6)
        assert samples["left_tracked"].all() and samples["right_tracked"].all()
        assert samples["left_pupil"].median() == pytest.approx(3.0)
        assert samples["right_pupil"].median() == pytest.approx(4.0)

        # Vergence is the caller's one line, and it comes out where it was put.
        vergence = samples["left_x_dva"] - samples["right_x_dva"]
        assert vergence.median() == pytest.approx(1.0, abs=1e-6)

    def test_one_eye_lost_does_not_lose_the_other(self, tmp_path):
        """The case a single `tracked` flag would hide. Encoding loss only as
        NaN also loses the surviving eye through any arithmetic that touches
        both, since (finite + nan) / 2 is nan — so the position is NaN AND the
        per-eye boolean says which eye it was."""
        builder = RunBuilder(tmp_path)
        builder.trial_of(duration_s=0.25, eye_lost_s={"left": (0.0, 0.025)})
        samples = read_run_binocular(builder.write()).samples

        lost = ~samples["left_tracked"]
        assert lost.sum() == pytest.approx(0.025 * SAMPLE_HZ, abs=1)
        # The left eye is gone for those samples...
        assert samples.loc[lost, "left_x_dva"].isna().all()
        assert samples.loc[lost, "left_pupil"].isna().all()
        # ...and the right eye is there for every one of them, which is the
        # whole point: a monocular epoch stays usable and stays findable.
        assert samples["right_tracked"].all()
        assert samples.loc[lost, "right_x_dva"].notna().all()
        assert len(samples) == int(0.25 * SAMPLE_HZ)

    def test_a_blink_loses_both_eyes(self, tmp_path):
        builder = RunBuilder(tmp_path)
        builder.trial_of(duration_s=0.25, blink_s=(0.1, 0.15))
        samples = read_run_binocular(builder.write()).samples

        blinking = ~samples["left_tracked"]
        assert blinking.sum() == pytest.approx(0.05 * SAMPLE_HZ, abs=1)
        assert (~samples.loc[blinking, "right_tracked"]).all()

    def test_it_does_not_need_to_be_told_which_eye_the_session_read(self, tmp_path):
        """EYE_USED records the eye the SESSION read online, for its fixation
        windows. The device recorded both regardless, so a binocular read has
        no use for it — and must not refuse a run that lacks it."""
        builder = RunBuilder(tmp_path)
        builder.trial_of(mark_eye_used=False)
        run = builder.write()

        assert read_run_binocular(run).samples["left_tracked"].all()
        # The monocular reader still insists, because it has to choose.
        with pytest.raises(DataError, match="no EYE_USED mark"):
            read_run(run)

    def test_the_views_are_the_same_ones_the_monocular_recording_offers(self, tmp_path):
        builder = RunBuilder(tmp_path)
        builder.trial_of(duration_s=0.1)
        builder.trial_of(duration_s=0.1)
        recording = read_run_binocular(builder.write())

        assert recording.sample_rate_hz == pytest.approx(SAMPLE_HZ, rel=1e-3)
        spans = recording.trial_spans()
        assert spans["trial_index"].tolist() == [1, 2]
        first = recording.between(spans["t_start"].iloc[0], spans["t_end"].iloc[0])
        assert 0 < len(first) <= len(recording.samples)
        assert event_times(recording.messages, "STIM_ON")["trial_index"].tolist() == [1, 2]

    def test_the_bounds_check_applies_per_eye_and_names_it(self, tmp_path):
        """Read in the wrong frame, both eyes are half a panel out; the error
        has to say which eye it measured so a one-eyed fault is findable."""
        builder = RunBuilder(tmp_path)
        builder.trial_of(gaze_px=(1100.0, 700.0))  # screen px, read as centred
        run = builder.write()

        with pytest.raises(DataError, match="left-eye samples") as error:
            read_run_binocular(run)
        assert "screen_y_down" in str(error.value)

        recording = read_run_binocular(run, gaze_frame="screen_y_down")
        assert recording.samples["left_x_dva"].median() == pytest.approx(140 / PX_PER_DEG, abs=1e-6)
        assert read_run_binocular(run, check_bounds=False).samples["left_tracked"].all()

    def test_the_real_recording_reads_with_both_eyes_untracked(self, tmp_path, caplog):
        """The fixture from the rig: made with no calibration on the device,
        so every position is NaN and both blink flags are set. The reader
        represents that faithfully and says so, rather than refusing it —
        whether an all-NaN run is analysable is the experiment's call, not
        the reader's."""
        import logging

        for path in FIXTURES.glob("*.csv"):
            shutil.copy(path, tmp_path / path.name)
        write_snapshot(tmp_path)

        with caplog.at_level(logging.WARNING, logger="alhazen.analysis.io.viewpixx"):
            recording = read_run_binocular(tmp_path)

        assert len(recording.samples) == 40
        assert not recording.samples["left_tracked"].any()
        assert not recording.samples["right_tracked"].any()
        # One warning per eye, naming the likely cause.
        said = [r.getMessage() for r in caplog.records if "is tracked" in r.getMessage()]
        assert len(said) == 2
        assert any("no left-eye sample" in m for m in said)
        assert all("no calibration on the device" in m for m in said)

    def test_an_empty_recording_is_reported_whatever_the_caller_thinks_of_the_edges(
        self, tmp_path, caplog
    ):
        """`check_bounds=False` means "this run legitimately goes off the
        panel" — the pursuit case — and it must not also switch off "this
        recording is empty". It did, and the caller it was recommended to
        was the one who lost the message."""
        import logging

        for path in FIXTURES.glob("*.csv"):
            shutil.copy(path, tmp_path / path.name)
        write_snapshot(tmp_path)

        with caplog.at_level(logging.WARNING, logger="alhazen.analysis.io.viewpixx"):
            read_run_binocular(tmp_path, check_bounds=False)
            read_run(tmp_path, check_bounds=False)

        said = [r.getMessage() for r in caplog.records if "is tracked" in r.getMessage()]
        # Two eyes from the binocular read, one from the monocular.
        assert len(said) == 3
