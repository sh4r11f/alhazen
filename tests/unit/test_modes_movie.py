"""Movie mode: the recorder every experiment shares.

The experiment supplies pixels; this module turns them into files. What can
go wrong on this side of the contract is exactly what these tests pin: a
frame quietly clipped instead of refused, an odd dimension the encoder
rejects after the fact, a sheet whose panels are not commensurable, a filter
that silently matched nothing, and a written file whose length is not the
stream's.

The encoder tests write real mp4 files and read them back. Deliberately no
skip: the recorder needs no screen, so nothing stops a test environment from
exercising it, and a writer whose only test skips itself is a writer nobody
is testing.
"""

from __future__ import annotations

import numpy as np
import pytest
import yaml

from alhazen.config.models import Model
from alhazen.core.events import EventSchema
from alhazen.core.trial import outcomes
from alhazen.errors import ConfigError
from alhazen.modes import movie
from alhazen.modes.movie import MovieClip, MovieSetup, run_movie
from alhazen.task.task import Task

HZ = 30.0


def grey(value: float, shape=(24, 32)) -> np.ndarray:
    return np.full(shape, value, dtype=np.float32)


def clip_of(frames: list[np.ndarray], name: str = "clip", label: str | None = None) -> MovieClip:
    """A clip over a fixed list. `list(frames)` in the lambda hands each call
    its own copy, which is the restartability the dataclass promises."""
    return MovieClip(name=name, frames=lambda: iter(list(frames)), label=label)


def read_back(path):
    import imageio.v2 as imageio

    return imageio.mimread(path)


@pytest.fixture
def rig(tmp_path):
    """A rig config as run_movie sees one — only the monitor matters to it."""
    from alhazen.config.loader import load_rig

    path = tmp_path / "rig.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "monitor": {
                    "width_px": 32,
                    "height_px": 24,
                    "width_cm": 32,
                    "distance_cm": 57,
                    "refresh_rate_hz": HZ,
                },
                "display": {"backend": "simulated"},
                "data_root": str(tmp_path / "data"),
            }
        )
    )
    return load_rig(path)


class TestTheFrameContract:
    def test_float_luminance_maps_zero_to_black_and_one_to_white(self):
        assert movie.to_uint8(grey(0.0), "c").max() == 0
        assert movie.to_uint8(grey(1.0), "c").min() == 255
        assert movie.to_uint8(grey(0.0), "c").dtype == np.uint8

    def test_uint8_passes_through_untouched(self):
        frame = np.full((4, 6), 200, dtype=np.uint8)
        assert movie.to_uint8(frame, "c") is frame

    def test_rgb_is_accepted_in_both_dtypes(self):
        assert movie.to_uint8(np.zeros((4, 6, 3), np.float32), "c").shape == (4, 6, 3)
        assert movie.to_uint8(np.zeros((4, 6, 3), np.uint8), "c").shape == (4, 6, 3)

    def test_a_float_outside_zero_one_is_refused_naming_the_clip(self):
        """Clipped quietly, a compositing bug ships in a movie that looks
        merely 'a bit off' — the worst place to discover a rendering error."""
        with pytest.raises(ConfigError, match="'hot'.*outside 0..1"):
            movie.to_uint8(grey(1.5), "hot")

    def test_nan_is_refused(self):
        with pytest.raises(ConfigError, match="NaN"):
            movie.to_uint8(grey(float("nan")), "c")

    def test_a_shape_that_is_not_a_frame_is_refused(self):
        with pytest.raises(ConfigError, match="shape"):
            movie.to_uint8(np.zeros((4, 6, 4)), "c")  # RGBA is not the contract
        with pytest.raises(ConfigError, match="shape"):
            movie.to_uint8(np.zeros(24), "c")

    def test_an_integer_dtype_other_than_uint8_is_refused(self):
        with pytest.raises(ConfigError, match="int32|int64"):
            movie.to_uint8(np.zeros((4, 6), np.int32), "c")


class TestEvenDimensions:
    """h264 refuses an odd width or height outright, writing nothing."""

    def test_odd_dimensions_grow_by_one_row_or_column_of_edge(self):
        frame = np.arange(15, dtype=np.uint8).reshape(3, 5)
        padded = movie.even_dims(frame)
        assert padded.shape == (4, 6)
        assert np.array_equal(padded[3], padded[2])  # the added line is the edge
        assert np.array_equal(padded[:, 5], padded[:, 4])

    def test_even_dimensions_are_untouched(self):
        frame = np.zeros((4, 6), np.uint8)
        assert movie.even_dims(frame) is frame

    def test_rgb_keeps_its_channels(self):
        assert movie.even_dims(np.zeros((3, 5, 3), np.uint8)).shape == (4, 6, 3)


class TestScaling:
    def test_half_scale_halves_both_dimensions(self):
        frame = np.full((24, 32), 90, np.uint8)
        small = movie.scale_frame(frame, 0.5)
        assert small.shape == (12, 16)
        # Area averaging of a flat frame is the same flat frame.
        assert np.all(small == 90)

    def test_scale_one_is_the_identity(self):
        frame = np.zeros((4, 6), np.uint8)
        assert movie.scale_frame(frame, 1.0) is frame

    def test_enlarging_or_a_nonsense_scale_is_refused(self):
        """A movie is for looking at, and upsampling invents pixels the
        display never had."""
        for scale in (0.0, -0.5, 1.5):
            with pytest.raises(ConfigError, match="scale"):
                movie.scale_frame(np.zeros((4, 6), np.uint8), scale)


class TestRecordingOneClip:
    def test_the_file_holds_every_frame_at_the_rig_rate(self, tmp_path):
        import imageio.v2 as imageio

        path = tmp_path / "out" / "clip.mp4"
        written = movie.record_clip(clip_of([grey(0.2)] * 5), HZ, path)

        assert written == 5
        assert len(read_back(path)) == 5
        assert imageio.get_reader(path).get_meta_data()["fps"] == pytest.approx(HZ)

    def test_odd_sized_frames_still_encode(self, tmp_path):
        """The whole point of even_dims: 33x25 would be refused by h264 with
        an ffmpeg error about divisibility, and nothing would be written."""
        path = tmp_path / "odd.mp4"
        movie.record_clip(clip_of([grey(0.5, (25, 33))] * 2), HZ, path)
        frames = read_back(path)
        assert frames[0].shape[:2] == (26, 34)

    def test_a_clip_with_no_frames_is_refused_and_leaves_no_file(self, tmp_path):
        """A zero-frame mp4 in the listing reads as an encoder problem and
        sends someone debugging ffmpeg instead of their own generator."""
        path = tmp_path / "empty.mp4"
        with pytest.raises(ConfigError, match="'empty'.*no frames"):
            movie.record_clip(clip_of([], name="empty"), HZ, path)
        assert not path.exists()

    def test_a_bad_frame_mid_stream_still_names_the_clip(self, tmp_path):
        with pytest.raises(ConfigError, match="'broken'"):
            movie.record_clip(
                clip_of([grey(0.5), grey(2.0)], name="broken"), HZ, tmp_path / "b.mp4"
            )


class TestTheSheet:
    def three_clips(self):
        return [
            clip_of([grey(0.0)] * 4, name="a"),
            clip_of([grey(0.5)] * 4, name="b"),
            clip_of([grey(1.0)] * 4, name="c"),
        ]

    def test_panels_tile_into_a_labelled_grid(self, tmp_path):
        path = tmp_path / "sheet.mp4"
        written = movie.record_sheet(self.three_clips(), HZ, path, columns=2)

        frames = read_back(path)
        assert written == len(frames) == 4
        # Three clips in two columns is two rows, each a panel plus its
        # label strip; even_dims may add one more pixel each way. The strip
        # is sized from the longest caption, which for a/b/c is the first.
        _, label_h = movie._fit_font("a", 32)
        expected_w, expected_h = 2 * 32, 2 * (24 + label_h)
        height, width = frames[0].shape[:2]
        assert width == expected_w + expected_w % 2
        assert height == expected_h + expected_h % 2
        # The label strip is really there: taller than the panels alone.
        assert height > 2 * 24

    def test_a_clip_that_ends_early_holds_its_last_frame(self, tmp_path):
        """A trial's end state is part of what it shows; a panel that blinks
        to black reads as a dropped stream rather than a shorter trial."""
        clips = [
            clip_of([grey(1.0)] * 2, name="short"),  # white, ends first
            clip_of([grey(0.0)] * 6, name="long"),
        ]
        path = tmp_path / "sheet.mp4"
        written = movie.record_sheet(clips, HZ, path, columns=2)
        assert written == 6

        last = read_back(path)[-1]
        _, label_h = movie._fit_font("short", 32)
        # The short clip's panel (left column, under its label) is still
        # white on the final frame. Lossy encoding, so "white-ish".
        panel = last[label_h : label_h + 24, 0:32]
        assert panel.mean() > 180

    def test_panels_of_different_sizes_are_refused_by_name(self, tmp_path):
        """A sheet that fitted each panel to its own content would resize
        away exactly the differences a condition grid is built to show."""
        clips = [clip_of([grey(0.5)], name="a"), clip_of([grey(0.5, (10, 10))], name="b")]
        with pytest.raises(ConfigError, match="'a'.*'b'"):
            movie.record_sheet(clips, HZ, tmp_path / "s.mp4", columns=2)

    def test_one_grey_panel_promotes_the_sheet_to_rgb(self, tmp_path):
        clips = [
            clip_of([np.zeros((24, 32, 3), np.float32)], name="colour"),
            clip_of([grey(0.5)], name="grey"),
        ]
        movie.record_sheet(clips, HZ, tmp_path / "s.mp4", columns=2)
        assert read_back(tmp_path / "s.mp4")[0].ndim == 3

    def test_the_caption_is_the_label_when_one_is_given(self):
        assert clip_of([], name="x", label="the x condition").caption == "the x condition"
        assert clip_of([], name="x").caption == "x"


class TestRunMovie:
    def hook(self, clips):
        return lambda setup: clips

    def test_one_file_per_clip_lands_under_out(self, rig, tmp_path):
        out = tmp_path / "movies"
        lines = []
        code = run_movie(
            self.hook([clip_of([grey(0.3)] * 2, "a"), clip_of([grey(0.6)] * 2, "b")]),
            rig=rig,
            params=None,
            out=out,
            echo=lines.append,
        )
        assert code == 0
        assert (out / "a.mp4").exists() and (out / "b.mp4").exists()
        # The report names every file and its duration on the rig's clock.
        assert any("a.mp4" in line for line in lines)
        assert any(f"{2 / HZ:.2f} s" in line for line in lines)

    def test_the_setup_carries_the_rigs_own_geometry_and_clock(self, rig, tmp_path):
        seen = {}

        def hook(setup: MovieSetup):
            seen["setup"] = setup
            return [clip_of([grey(0.5)], "a")]

        run_movie(hook, rig=rig, params="the-params", out=tmp_path, echo=lambda _: None)
        setup = seen["setup"]
        assert setup.hz == HZ
        assert setup.screen.width_px == rig.monitor.width_px
        assert setup.params == "the-params"

    def test_clip_selects_by_name(self, rig, tmp_path):
        run_movie(
            self.hook([clip_of([grey(0.3)], "a"), clip_of([grey(0.6)], "b")]),
            rig=rig,
            params=None,
            out=tmp_path,
            clip_names=("b",),
            echo=lambda _: None,
        )
        assert (tmp_path / "b.mp4").exists()
        assert not (tmp_path / "a.mp4").exists()

    def test_a_filter_that_matches_nothing_is_refused_with_the_list(self, rig, tmp_path):
        """A filter that silently matched nothing would look like a working
        command that produced no output."""
        with pytest.raises(ConfigError, match="'c'.*records a, b"):
            run_movie(
                self.hook([clip_of([grey(0.3)], "a"), clip_of([grey(0.6)], "b")]),
                rig=rig,
                params=None,
                out=tmp_path,
                clip_names=("c",),
                echo=lambda _: None,
            )

    def test_duplicate_clip_names_are_refused(self, rig, tmp_path):
        with pytest.raises(ConfigError, match="twice.*'a'"):
            run_movie(
                self.hook([clip_of([grey(0.3)], "a"), clip_of([grey(0.6)], "a")]),
                rig=rig,
                params=None,
                out=tmp_path,
                echo=lambda _: None,
            )

    def test_a_name_that_cannot_be_a_filename_is_refused(self, rig, tmp_path):
        with pytest.raises(ConfigError, match="cannot be a filename"):
            run_movie(
                self.hook([clip_of([grey(0.3)], "up/../and-away")]),
                rig=rig,
                params=None,
                out=tmp_path,
                echo=lambda _: None,
            )

    def test_no_clips_at_all_is_refused(self, rig, tmp_path):
        with pytest.raises(ConfigError, match="no clips"):
            run_movie(self.hook([]), rig=rig, params=None, out=tmp_path, echo=lambda _: None)

    def test_sheet_writes_one_file_instead_of_many(self, rig, tmp_path):
        sheet = tmp_path / "all.mp4"
        run_movie(
            self.hook([clip_of([grey(0.3)] * 2, "a"), clip_of([grey(0.6)] * 2, "b")]),
            rig=rig,
            params=None,
            out=tmp_path,
            sheet=sheet,
            echo=lambda _: None,
        )
        assert sheet.exists()
        assert not (tmp_path / "a.mp4").exists()


class MovieParams(Model):
    pass


class RecordableTask(Task):
    """The smallest task that answers movie mode."""

    name = "recordable"
    events = EventSchema(())
    outcomes = outcomes(DONE=dict(completed=True, success=True))
    params_model = MovieParams

    def movie_clips(self, setup):
        # Two flips of mid-grey: enough to prove the whole path from
        # `--mode movie` to a file on disk.
        return [MovieClip(name="main", frames=lambda: iter([grey(0.5)] * 2))]


class SilentTask(Task):
    name = "silent"
    events = EventSchema(())
    outcomes = outcomes(DONE=dict(completed=True, success=True))
    params_model = MovieParams


class TestTheModeEndToEnd:
    """Through `run_experiment`, exactly as an experiment's run.py starts it."""

    def rig_yaml(self, tmp_path):
        path = tmp_path / "rig.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "monitor": {
                        "width_px": 32,
                        "height_px": 24,
                        "width_cm": 32,
                        "distance_cm": 57,
                        "refresh_rate_hz": HZ,
                    },
                    "display": {"backend": "simulated"},
                    "data_root": str(tmp_path / "data"),
                }
            )
        )
        return path

    def test_mode_movie_writes_the_files_and_no_data(self, tmp_path):
        from alhazen.cli.modes import run_experiment

        out = tmp_path / "movies"
        code = run_experiment(
            task_class=RecordableTask,
            default_rig=self.rig_yaml(tmp_path),
            argv=["--mode", "movie", "--out", str(out)],
        )
        assert code == 0
        assert (out / "main.mp4").exists()
        assert not (tmp_path / "data").exists()  # a movie is not a session

    def test_sheet_without_a_path_lands_beside_the_clips(self, tmp_path):
        from alhazen.cli.modes import run_experiment

        out = tmp_path / "movies"
        code = run_experiment(
            task_class=RecordableTask,
            default_rig=self.rig_yaml(tmp_path),
            argv=["--mode", "movie", "--out", str(out), "--sheet"],
        )
        assert code == 0
        assert (out / "all-clips.mp4").exists()

    def test_a_task_without_clips_says_what_to_implement(self, tmp_path, capsys):
        from alhazen.cli.modes import run_experiment

        code = run_experiment(
            task_class=SilentTask,
            default_rig=self.rig_yaml(tmp_path),
            argv=["--mode", "movie"],
        )
        assert code == 2
        err = capsys.readouterr().err
        assert "CANNOT RECORD" in err and "movie_clips" in err

    def test_a_config_problem_exits_one_not_a_traceback(self, tmp_path, capsys):
        from alhazen.cli.modes import run_experiment

        code = run_experiment(
            task_class=RecordableTask,
            default_rig=self.rig_yaml(tmp_path),
            argv=["--mode", "movie", "--clip", "nope"],
        )
        assert code == 1
        assert "CANNOT RECORD" in capsys.readouterr().err

    def test_the_default_task_hook_raises_with_instructions(self):
        with pytest.raises(NotImplementedError, match="movie_clips"):
            SilentTask(MovieParams()).movie_clips(None)


class TestTheModeContract:
    def test_movie_neither_runs_trials_nor_writes_real_data(self):
        """Recorded here because data placement hangs off these flags: a
        movie must never claim a run directory."""
        from alhazen.modes import Mode

        assert not Mode.MOVIE.runs_trials
        assert not Mode.MOVIE.writes_real_data
        assert Mode.MOVIE.summary  # every mode explains itself in --help
