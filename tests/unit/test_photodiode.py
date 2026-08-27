"""The photodiode patch marks exactly the flip that carries an armed event."""

from __future__ import annotations

import pytest

from alhazen.analysis.sync import AlignmentFit
from alhazen.config.models import PhotodiodeConfig
from alhazen.errors import DataError
from alhazen.stimuli.photodiode import PhotodiodePatch, make_photodiode
from alhazen.testing import FakeClock, FakeDisplay
from support import COMPLETED, FRAME_S, SCREEN, EngineHarness, RunForFrames


def make_patch(events=("STIM_ON",), corner="br", size_px=60):
    display = FakeDisplay(FakeClock(), FRAME_S)
    return PhotodiodePatch(display, SCREEN, corner=corner, size_px=size_px, events=events)


class TestPatchStates:
    def test_white_only_when_an_armed_event_is_queued(self):
        patch = make_patch()
        patch.draw([])
        patch.draw(["STIM_ON"])
        patch.draw(["FIX_ON"])  # queued, but not armed on this rig
        assert patch.states == [False, True, False]

    def test_drawn_on_every_frame(self):
        # Black frames are drawn too, not skipped: the corner's mean
        # luminance must be controlled, and the diode needs a clean two-level
        # signal rather than a patch appearing from nowhere.
        patch = make_patch()
        for _ in range(5):
            patch.draw([])
        assert len(patch.states) == 5

    def test_corners_are_flush_and_mirrored(self):
        from alhazen.stimuli.photodiode import _corner_pos

        assert _corner_pos(SCREEN, "br", 60) == (930.0, -510.0)
        assert _corner_pos(SCREEN, "tl", 60) == (-930.0, 510.0)
        assert _corner_pos(SCREEN, "tr", 60) == (930.0, 510.0)
        assert _corner_pos(SCREEN, "bl", 60) == (-930.0, -510.0)

    def test_factory_reads_the_rig_config(self):
        display = FakeDisplay(FakeClock(), FRAME_S)
        cfg = PhotodiodeConfig(corner="tl", size_px=40, events=["FIX_ON"])
        patch = make_photodiode(display, SCREEN, cfg)
        assert patch.armed_events == frozenset({"FIX_ON"})


class TestPatchInTheFrameLoop:
    def test_patch_is_white_on_exactly_the_marked_frame(self):
        patch = make_patch()
        harness = EngineHarness(
            overlay=lambda ctx: patch.draw(name for name, _ in ctx.pending_flip_events)
        )

        class EmitOnFrameTwo(RunForFrames):
            def on_frame(self, ctx):
                if len(self.frames_seen) == 2:
                    ctx.emit_on_flip("STIM_ON")
                return super().on_frame(ctx)

        harness.engine.run_trial(harness.ctx(), [EmitOnFrameTwo(4, COMPLETED)])

        # The overlay runs after the phase and before the flip, so the white
        # frame is the one whose flip carries STIM_ON — the same flip the
        # event's timestamp refers to.
        assert patch.states == [False, False, True, False, False]
        (stim_on,) = [e for e in harness.collector.events if e.name == "STIM_ON"]
        assert stim_on.t == (patch.states.index(True) + 1) * FRAME_S


class TestNoisyChannelIsRefused:
    """An unconnected analog channel is noise. `find_edges` sets its threshold
    from the trace's own min/max, so noise crosses it thousands of times and
    every armed event finds an edge microseconds later: a confident, plausible
    near-zero display latency that an analysis would subtract from every
    timestamp."""

    class FakeRun:
        run_dir = "<fake>"
        photodiode_events = ["STIM_ON"]

        def __init__(self, times):
            self._times = times

        def event_times(self, name):
            return list(self._times) if name == "STIM_ON" else []

    def measure(self, tmp_path, n_events, edge_times, monkeypatch):
        import numpy as np

        from alhazen.analysis import photodiode as module

        rate = 1000.0
        n_samples = int(max(edge_times, default=1.0) * rate) + 100
        trace = np.zeros(n_samples)
        for t in edge_times:
            index = int(t * rate)
            trace[index : index + 2] = 1.0
        monkeypatch.setattr(
            module.spikeglx, "find_run_files", lambda _d: {"bin_path": None, "meta_path": None}
        )
        monkeypatch.setattr(
            module.spikeglx, "analog_channel", lambda _b, _m, channel=0: (trace, rate)
        )
        events = [1.0 + index * 1.0 for index in range(n_events)]
        alignment = AlignmentFit(
            event="STIM_ON",
            offset_s=0.0,
            scale=1.0,
            t0_behavior_s=0.0,
            n_behavior=n_events,
            n_pulses=n_events,
            n_matched=n_events,
            residual_rms_ms=0.0,
            residual_max_ms=0.0,
        )
        return module.measure_from_recording(
            self.FakeRun(events), tmp_path, alignment, analog_channel=2
        )

    def test_an_unconnected_channel_of_pure_noise_is_refused(self, tmp_path, monkeypatch):
        """The exact failure: white noise on a channel nobody plugged in."""
        import numpy as np

        from alhazen.analysis import photodiode as module

        rate = 1000.0
        noise = np.random.default_rng(4).normal(0.0, 1.0, 30_000)
        monkeypatch.setattr(
            module.spikeglx, "find_run_files", lambda _d: {"bin_path": None, "meta_path": None}
        )
        monkeypatch.setattr(
            module.spikeglx, "analog_channel", lambda _b, _m, channel=0: (noise, rate)
        )
        events = [1.0 + index * 1.0 for index in range(8)]
        alignment = AlignmentFit(
            event="STIM_ON",
            offset_s=0.0,
            scale=1.0,
            t0_behavior_s=0.0,
            n_behavior=8,
            n_pulses=8,
            n_matched=8,
            residual_rms_ms=0.0,
            residual_max_ms=0.0,
        )

        with pytest.raises(DataError, match="not carrying the photodiode"):
            module.measure_from_recording(
                self.FakeRun(events), tmp_path, alignment, analog_channel=3
            )

    def test_far_too_many_edges_names_the_channel(self, tmp_path, monkeypatch):
        # 5 armed events, 100 edges: twenty per event.
        edges = [1.0 + i * 0.05 for i in range(100)]
        with pytest.raises(DataError, match="analog channel 2"):
            self.measure(tmp_path, 5, edges, monkeypatch)

    def test_one_edge_per_event_is_accepted(self, tmp_path, monkeypatch):
        edges = [1.005 + index * 1.0 for index in range(5)]
        report = self.measure(tmp_path, 5, edges, monkeypatch)
        assert report.n_matched == 5

    def test_a_little_extra_is_tolerated(self, tmp_path, monkeypatch):
        # A stray edge or two must not refuse an otherwise good channel.
        edges = [1.005 + index * 1.0 for index in range(5)] + [0.2, 0.4]
        report = self.measure(tmp_path, 5, edges, monkeypatch)
        assert report.n_matched == 5
