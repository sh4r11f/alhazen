"""The RF-mapping template: schedule, phase, live map, presets, modes.

The centrepiece is the closed loop: a ProbeSequence flashing through a real
TrialEngine, a SimulatedSpikeSource with ground-truth fields listening on
the same bus, and the LiveRFMap recovering those fields — known answer in,
same answer out, with no hardware and no renderer anywhere."""

from __future__ import annotations

import json

import numpy as np
import pytest

from alhazen.config.models import Duration, SpikeSourceConfig
from alhazen.core.events import Event
from alhazen.core.trial import CircleRegion, InputFrame
from alhazen.devices.spikes import SimulatedSpikeSource
from alhazen.errors import ConfigError
from alhazen.neural.rfmap import ProbeGrid
from alhazen.paradigms.base import Condition
from alhazen.stimuli.base import NullStimulus
from alhazen.task.live import LiveWiring
from alhazen.task.plan import TrialSetup
from alhazen.task.templates.rf_mapping import (
    LiveRFMap,
    MTRFMapTask,
    ProbeSchedule,
    ProbeSequence,
    ProbeSpec,
    RecordingProbe,
    RFMapParams,
    RFMapTask,
    V1RFMapTask,
    V2RFMapTask,
    V4RFMapTask,
)
from alhazen.testing import FakeClock, FakeDisplay
from support import FRAME_S, SCREEN, EngineHarness, make_session_config


def specs(grid: ProbeGrid, cells: list[tuple[int, int]]) -> list[ProbeSpec]:
    return [ProbeSpec(col, row, *grid.cell_center_dva(col, row), "bright") for col, row in cells]


def sequence_ctx(harness: EngineHarness, **kwargs):
    return harness.ctx(
        stimuli={"fixation": NullStimulus("fixation"), "probe": RecordingProbe()},
        regions={"fixation": CircleRegion((0.0, 0.0), 80.0)},
        **kwargs,
    )


DONE = RFMapTask.outcomes["COMPLETED"]
BREAK = RFMapTask.outcomes["FIX_BREAK"]


class TestProbeSequence:
    def run_sequence(self, probes, input_provider=None, **phase_kwargs):
        harness = EngineHarness(
            declared_events=("PROBE_ON",),
            input_provider=input_provider or (lambda: InputFrame(gaze=(0.0, 0.0))),
        )
        phase = ProbeSequence(
            probes,
            flash_frames=phase_kwargs.pop("flash_frames", 2),
            isi_frames=phase_kwargs.pop("isi_frames", 1),
            tail_frames=phase_kwargs.pop("tail_frames", 3),
            on_break=BREAK,
            on_done=DONE,
            **phase_kwargs,
        )
        ctx = sequence_ctx(harness)
        result = harness.engine.run_trial(ctx, [phase])
        return harness, ctx, result

    def test_all_probes_flash_and_the_log_is_flip_honest(self):
        grid = ProbeGrid.from_extent(4, 4, 8.0, 8.0)
        probes = specs(grid, [(0, 0), (3, 3), (1, 2)])
        harness, ctx, result = self.run_sequence(probes)

        assert result.outcome is DONE
        assert ctx.record["rf_n_probes_shown"] == 3
        emitted = [e for e in harness.collector.events if e.name == "PROBE_ON"]
        assert [e.payload["col"] for e in emitted] == [0, 3, 1]
        log = json.loads(ctx.record["rf_probes_json"])
        # The logged times ARE the emitted flip times, entry for entry.
        assert [entry["t"] for entry in log] == [e.t for e in emitted]
        assert [entry["polarity"] for entry in log] == ["bright"] * 3
        # The probe stimulus was placed once per flash, at the cell in px.
        placements = ctx.stimuli["probe"].placements
        assert len(placements) == 3
        assert placements[0][0] == pytest.approx(SCREEN.deg2px(probes[0].x_dva))

    def test_a_break_keeps_the_probes_already_shown(self):
        grid = ProbeGrid.from_extent(4, 4, 8.0, 8.0)
        probes = specs(grid, [(0, 0), (1, 0), (2, 0), (3, 0)])
        # Fixation holds for 8 frames, then a blink: with isi 1 + flash 2,
        # frames 0..7 cover probes 0 and 1 and the onset of probe 2.
        calls = iter(range(1000))

        def blinky() -> InputFrame:
            return InputFrame(gaze=(0.0, 0.0) if next(calls) < 8 else None)

        harness, ctx, result = self.run_sequence(probes, input_provider=blinky)
        assert result.outcome is BREAK
        shown = ctx.record["rf_n_probes_shown"]
        emitted = [e for e in harness.collector.events if e.name == "PROBE_ON"]
        # The record and the event stream must agree exactly: every probe
        # that flipped is in the log, and none that did not.
        assert shown == len(json.loads(ctx.record["rf_probes_json"]))
        assert shown in (2, 3)
        # A probe queued on the break frame itself may have flipped once;
        # the log may lag the stream by at most that one in-flight probe.
        assert len(emitted) - shown in (0, 1)

    def test_constructor_refuses_a_zero_frame_flash(self):
        grid = ProbeGrid.from_extent(2, 2, 4.0, 4.0)
        with pytest.raises(ValueError, match="flash_frames"):
            ProbeSequence(
                specs(grid, [(0, 0)]),
                flash_frames=0,
                isi_frames=1,
                tail_frames=1,
                on_break=BREAK,
                on_done=DONE,
            )
        with pytest.raises(ValueError, match="at least one probe"):
            ProbeSequence(
                [], flash_frames=1, isi_frames=1, tail_frames=1, on_break=BREAK, on_done=DONE
            )


class TestProbeSchedule:
    def schedule(self, cols=2, rows=2, reps=2, per_trial=3, polarity="both"):
        return ProbeSchedule(
            ProbeGrid.from_extent(cols, rows, 4.0, 4.0),
            n_reps_per_cell=reps,
            polarity=polarity,
            probes_per_trial=per_trial,
            rng=np.random.default_rng(0),
        )

    class Result:
        def __init__(self, shown: int) -> None:
            self.record = {"rf_n_probes_shown": shown}

    def test_every_repetition_served_exactly_once_when_all_shown(self):
        schedule = self.schedule()
        served = []
        while schedule.next() is not None:
            batch = schedule.take(3)
            served.extend(batch)
            schedule.record(None, self.Result(len(batch)))
        assert len(served) == 2 * 2 * 2
        # Each cell got exactly its repetitions, one bright and one dark.
        for col in range(2):
            for row in range(2):
                mine = [p for p in served if (p.col, p.row) == (col, row)]
                assert sorted(p.polarity for p in mine) == ["bright", "dark"]

    def test_unshown_probes_requeue_at_the_back(self):
        schedule = self.schedule()
        first = schedule.take(3)
        schedule.record(None, self.Result(1))  # only the first was shown
        assert schedule.remaining == 8 - 1
        served = []
        while schedule.next() is not None:
            batch = schedule.take(3)
            served.extend(batch)
            schedule.record(None, self.Result(len(batch)))
        # The two unshown probes came back — at the end, not immediately.
        assert served[-2:] == first[1:] or first[1] in served[-2:]
        counts = schedule.summary()
        assert counts["shown"].sum() == 8

    def test_take_twice_and_overreport_are_programming_errors(self):
        schedule = self.schedule()
        schedule.take(2)
        with pytest.raises(RuntimeError, match="take"):
            schedule.take(2)
        with pytest.raises(RuntimeError, match="disagree"):
            schedule.record(None, self.Result(5))

    def test_single_polarity_serves_only_that_polarity(self):
        schedule = self.schedule(polarity="dark")
        assert {p.polarity for p in schedule.take(8)} == {"dark"}


def make_setup(tmp_path) -> TrialSetup:
    return TrialSetup(
        cfg=make_session_config(tmp_path),
        screen=SCREEN,
        display=FakeDisplay(FakeClock(), FRAME_S),
        rng=np.random.default_rng(1),
        refresh_rate_hz=60.0,
        trial_index=1,
        attempt=1,
        condition=Condition({}),
    )


class TestBuildTrial:
    def build(self, tmp_path, task_cls=V1RFMapTask, **param_overrides):
        params = task_cls.params_model(**param_overrides)
        task = task_cls(params)
        task.make_source(params, np.random.default_rng(0))
        return task, task.build_trial(make_setup(tmp_path))

    def test_plan_shape(self, tmp_path):
        task, plan = self.build(tmp_path)
        assert [type(p).__name__ for p in plan.phases] == [
            "AcquireFixation",
            "HoldFixation",
            "ProbeSequence",
        ]
        assert set(plan.stimuli) == {"fixation", "probe"}
        assert plan.record["rf_n_probes_planned"] == 12
        assert "fixation" in plan.regions

    def test_zero_frame_flash_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="0 frames"):
            self.build(tmp_path, flash=Duration(ms=2))

    def test_build_trial_without_a_schedule_is_loud(self, tmp_path):
        task = V1RFMapTask(V1RFMapTask.params_model())
        with pytest.raises(RuntimeError, match="make_source"):
            task.build_trial(make_setup(tmp_path))


class TestLiveMapRecoversGroundTruth:
    def make_source(self, centers) -> tuple[SimulatedSpikeSource, FakeClock]:
        clock = FakeClock()
        source = SimulatedSpikeSource(
            SpikeSourceConfig(
                backend="simulated",
                sim_channels=len(centers),
                sim_rf_centers_dva=tuple(centers),
                sim_rf_sigma_dva=1.0,
                sim_baseline_hz=1.0,
                sim_peak_hz=300.0,
                sim_latency_ms=40.0,
                sim_duration_ms=50.0,
            )
        )
        source.configure(clock)
        source.connect()
        source.start()
        return source, clock

    def test_known_fields_in_same_fields_out(self):
        params = RFMapParams(
            grid_cols=5,
            grid_rows=5,
            grid_extent_x_dva=10.0,
            grid_extent_y_dva=10.0,
            window_start_ms=20.0,
            window_end_ms=110.0,
            n_display_maps=2,
        )
        grid = params.grid
        # Ground truth on two cell centres: (3,3) -> (+3,+3), (1,1) -> (-3,-3).
        source, clock = self.make_source([grid.cell_center_dva(3, 3), grid.cell_center_dva(1, 1)])
        live = LiveRFMap(params, source)

        t = 1.0
        for _rep in range(2):
            for row in range(5):
                for col in range(5):
                    x, y = grid.cell_center_dva(col, row)
                    event = Event(
                        name="PROBE_ON",
                        t=t,
                        trial_index=1,
                        payload={
                            "col": col,
                            "row": row,
                            "x_dva": x,
                            "y_dva": y,
                            "polarity": "bright",
                        },
                    )
                    source.on_event(event)
                    live.on_event(event)
                    t += 0.2
            clock.advance(t + 1.0 - clock.now())
            live.on_trial({})

        acc = live._accumulator
        assert acc is not None
        assert acc.n_flashes == 50
        assert acc.peak_cell(0) == (3, 3)
        assert acc.peak_cell(1) == (1, 1)
        cx, cy = acc.centroid_dva(0)
        assert cx == pytest.approx(3.0, abs=1.0)
        assert cy == pytest.approx(3.0, abs=1.0)
        # And the panel says the same thing, in JSON-safe form.
        (panel,) = live.panels()
        assert panel["title"] == "Receptive fields"
        data = panel["data"]
        assert data["form"] == "heatmap"
        json.dumps(panel, allow_nan=False)  # no NaN, no numpy scalars
        names = [m["name"] for m in data["maps"]]
        assert names[0] == "population"
        assert len(data["maps"][0]["matrix"]) == 5
        assert data["vmax"] > 0

    def test_finish_writes_the_npz_artifact(self, tmp_path):
        params = RFMapParams(grid_cols=3, grid_rows=3, window_end_ms=80.0)
        source, clock = self.make_source([(0.0, 0.0), (2.0, 2.0)])
        live = LiveRFMap(params, source)
        event = Event(
            name="PROBE_ON",
            t=0.5,
            trial_index=1,
            payload={"col": 1, "row": 1, "x_dva": 0.0, "y_dva": 0.0, "polarity": "bright"},
        )
        source.on_event(event)
        live.on_event(event)
        clock.advance(2.0)
        live.on_trial({})
        live.finish(tmp_path)

        saved = np.load(tmp_path / "rf_live_maps.npz")
        assert saved["counts"].shape == (2, 3, 3)
        assert saved["flashes"].sum() == 1
        assert saved["channel_ids"].tolist() == [0, 1]
        assert saved["n_unmapped_flashes"] == 0

    def test_without_a_spike_source_the_panel_says_so(self, tmp_path):
        live = LiveRFMap(RFMapParams(), None)
        live.on_trial({})  # a no-op, not a crash
        (panel,) = live.panels()
        assert panel["data"]["form"] == "empty"
        assert "devices.spikes" in panel["data"]["message"]
        live.finish(tmp_path)
        assert not (tmp_path / "rf_live_maps.npz").exists()

    def test_map_channels_naming_an_unmonitored_id_fails_at_build(self):
        source, _ = self.make_source([(0.0, 0.0), (2.0, 2.0)])
        with pytest.raises(ConfigError, match="map_channels"):
            LiveRFMap(RFMapParams(map_channels=(0, 99)), source)


class TestTaskSurface:
    def test_presets_scale_with_the_area(self):
        v1, v2, v4, mt = (
            cls.params_model() for cls in (V1RFMapTask, V2RFMapTask, V4RFMapTask, MTRFMapTask)
        )
        assert v1.probe_size_dva < v2.probe_size_dva < v4.probe_size_dva < mt.probe_size_dva
        assert v1.grid_extent_x_dva < v2.grid_extent_x_dva
        assert mt.grid_extent_x_dva == 20.0
        for preset in (v1, v2, v4, mt):
            assert preset.window_start_ms < preset.window_end_ms

    def test_task_names_are_registered_in_pyproject(self):
        # The entry points are what make `alhazen run --task rf-map-v1`
        # work; a preset missing from pyproject would import fine and be
        # undiscoverable on every rig.
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        text = pyproject.read_text()
        for name, cls in [
            ("rf-map-v1", "V1RFMapTask"),
            ("rf-map-v2", "V2RFMapTask"),
            ("rf-map-v4", "V4RFMapTask"),
            ("rf-map-mt", "MTRFMapTask"),
        ]:
            assert f'{name} = "alhazen.task.templates.rf_mapping:{cls}"' in text

    def test_simulation_supplies_a_fixating_tracker(self):
        simulation = V1RFMapTask(V1RFMapTask.params_model()).simulation(seed=3)
        assert simulation.tracker is not None
        assert not simulation.is_empty()

    def test_live_analysis_hook_returns_a_map_wired_to_the_source(self):
        task = V1RFMapTask(V1RFMapTask.params_model())
        source = SimulatedSpikeSource(SpikeSourceConfig(backend="simulated", sim_channels=2))
        live = task.live_analysis(LiveWiring(spikes=source, screen=SCREEN, clock=FakeClock()))
        assert isinstance(live, LiveRFMap)

    def test_movie_frames_are_composited_within_bounds(self):
        from alhazen.modes.movie import MovieSetup, to_uint8

        params = V1RFMapTask.params_model(probes_per_trial=4)
        task = V1RFMapTask(params)
        setup = MovieSetup(screen=SCREEN, hz=60.0, params=params, rng=np.random.default_rng(0))
        (clip,) = task.movie_clips(setup)
        frames = list(clip.frames())
        assert len(frames) == 24 * (params.flash.n_frames(60.0) + params.isi.n_frames(60.0))
        flashed = 0
        for frame in frames:
            assert frame.shape == (SCREEN.height_px, SCREEN.width_px)
            to_uint8(frame, clip.name)  # in 0..1, finite — or this raises
            # A frame with more than the fixation painted is a flash frame.
            if (frame != 0.5).sum() > 200:
                flashed += 1
        assert flashed > 0
        # Recording twice yields identical pixels (the sheet re-reads it).
        again = list(clip.frames())
        assert np.array_equal(frames[10], again[10])

    def test_params_validation_is_loud(self):
        with pytest.raises(ValueError, match="window"):
            RFMapParams(window_start_ms=100.0, window_end_ms=50.0)
        with pytest.raises(ValueError, match="1x1"):
            RFMapParams(grid_cols=0)
        with pytest.raises(ValueError, match="probes_per_trial"):
            RFMapParams(probes_per_trial=0)
