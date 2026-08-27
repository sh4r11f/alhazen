"""The readers, against synthetic files whose contents are known exactly."""

from __future__ import annotations

import numpy as np
import pytest

from alhazen.analysis.io import kilosort, spikeglx
from alhazen.analysis.io.eyelink import ensure_asc, read_asc
from alhazen.errors import DataError
from fixtures_neural import write_asc, write_kilosort, write_nidq


class TestSpikeGLX:
    def test_the_meta_gives_the_real_sample_rate(self, tmp_path):
        # Not the nominal one: the pilot rig's NI stream runs at 24999.92 Hz,
        # and 25000 drifts 3 ms over ten minutes.
        files = write_nidq(tmp_path / "run_g0", {0: [1.0]}, rate_hz=24999.92)
        meta = spikeglx.parse_meta(files["meta_path"])
        assert spikeglx.sample_rate_hz(meta) == pytest.approx(24999.92)

    def test_pulses_come_back_where_they_were_planted(self, tmp_path):
        planted = [0.5, 1.25, 2.0, 3.75]
        files = write_nidq(tmp_path / "run_g0", {2: planted}, duration_s=5.0)
        edges = spikeglx.digital_word_edges(files["bin_path"], files["meta_path"], bit_index=2)
        assert np.allclose(edges, planted, atol=1e-4)

    def test_each_line_is_read_independently(self, tmp_path):
        files = write_nidq(tmp_path / "run_g0", {0: [0.5, 1.5], 3: [1.0]}, duration_s=3.0)
        assert (
            len(spikeglx.digital_word_edges(files["bin_path"], files["meta_path"], bit_index=0))
            == 2
        )
        assert (
            len(spikeglx.digital_word_edges(files["bin_path"], files["meta_path"], bit_index=3))
            == 1
        )

    def test_an_edge_on_a_chunk_boundary_is_not_lost(self, tmp_path):
        # The bug this guards: reading in chunks without carrying the last
        # sample loses one edge per boundary — and a dropped pulse is exactly
        # what alignment cannot recover from.
        rate = 1000.0
        files = write_nidq(tmp_path / "run_g0", {1: [1.0, 2.0, 3.0]}, duration_s=4.0, rate_hz=rate)
        edges = spikeglx.digital_word_edges(
            files["bin_path"], files["meta_path"], bit_index=1, chunk_samples=1000
        )
        assert np.allclose(edges, [1.0, 2.0, 3.0], atol=1e-3)

    def test_falling_edges_too(self, tmp_path):
        files = write_nidq(tmp_path / "run_g0", {0: [1.0]}, duration_s=3.0, pulse_width_s=0.1)
        falling = spikeglx.digital_word_edges(
            files["bin_path"], files["meta_path"], bit_index=0, edge="falling"
        )
        assert falling == pytest.approx([1.1], abs=1e-3)

    def test_a_truncated_file_is_refused(self, tmp_path):
        files = write_nidq(tmp_path / "run_g0", {0: [1.0]}, duration_s=1.0)
        with files["bin_path"].open("ab") as handle:
            handle.write(b"\x00")  # one stray byte: no longer whole frames
        with pytest.raises(DataError, match="truncated"):
            spikeglx.n_samples(files["bin_path"], spikeglx.parse_meta(files["meta_path"]))

    def test_a_bit_outside_the_word_is_refused(self, tmp_path):
        files = write_nidq(tmp_path / "run_g0", {0: [1.0]})
        with pytest.raises(DataError, match="0..15"):
            spikeglx.digital_word_edges(files["bin_path"], files["meta_path"], bit_index=16)

    def test_finding_a_runs_files(self, tmp_path):
        write_nidq(tmp_path / "run_g0", {0: [1.0]})
        found = spikeglx.find_run_files(tmp_path / "run_g0")
        assert found["bin_path"].name.endswith(".nidq.bin")
        assert found["meta_path"].exists()

    def test_a_run_with_no_ni_stream_says_so(self, tmp_path):
        (tmp_path / "empty_g0").mkdir()
        with pytest.raises(DataError, match="no \\*.nidq.bin"):
            spikeglx.find_run_files(tmp_path / "empty_g0")

    def test_the_analog_channel_comes_back_with_its_rate(self, tmp_path):
        files = write_nidq(tmp_path / "run_g0", {0: [1.0]}, duration_s=2.0, analog_edges=[0.5, 1.5])
        trace, rate = spikeglx.analog_channel(files["bin_path"], files["meta_path"], channel=0)
        assert rate == pytest.approx(25000.0)
        assert trace.max() > 0


class TestKilosort:
    def test_spike_times_come_back_in_seconds(self, tmp_path):
        write_kilosort(tmp_path / "sort", {1: [0.1, 0.2], 2: [0.15]}, rate_hz=30000.0)
        data = kilosort.read_kilosort(tmp_path / "sort", sample_rate_hz=30000.0)
        assert data.unit_ids == [1, 2]
        assert np.allclose(np.sort(data.times_of(1)), [0.1, 0.2], atol=1e-5)

    def test_curation_labels_select_the_good_units(self, tmp_path):
        write_kilosort(tmp_path / "sort", {1: [0.1], 2: [0.2]}, good_units={2})
        data = kilosort.read_kilosort(tmp_path / "sort", sample_rate_hz=30000.0)
        assert data.good_units() == [2]

    def test_an_uncurated_sort_has_no_good_units(self, tmp_path):
        # Rather than treating every cluster as a unit, which would quietly
        # include the noise ones.
        write_kilosort(tmp_path / "sort", {1: [0.1]}, good_units=None)
        data = kilosort.read_kilosort(tmp_path / "sort", sample_rate_hz=30000.0)
        assert data.good_units() == []

    def test_an_unfinished_sort_says_which_file_is_missing(self, tmp_path):
        (tmp_path / "sort").mkdir()
        with pytest.raises(DataError, match="spike_times.npy"):
            kilosort.read_kilosort(tmp_path / "sort", sample_rate_hz=30000.0)


class TestEyeLinkAsc:
    def test_samples_and_messages_are_read(self, tmp_path):
        path = write_asc(
            tmp_path / "run.asc",
            samples=[(1000.0, 960.0, 540.0), (1001.0, 961.0, 541.0)],
            # Whole milliseconds, which is what an EyeLink actually writes.
            messages=[(999.0, "TRIALID 1"), (1001.0, "STIM_ON")],
        )
        recording = read_asc(path)
        assert recording.n_samples == 2
        assert recording.message_times("STIM_ON") == [1001.0]
        assert recording.messages_starting("TRIALID") == [(999.0, "TRIALID 1")]

    def test_a_blink_is_nan_not_a_position(self, tmp_path):
        # Zero would be a position at the screen's centre and the sentinel a
        # position off the planet; NaN is what every later mean and plot
        # needs to see.
        path = write_asc(
            tmp_path / "run.asc",
            samples=[(1000.0, 960.0, 540.0), (1001.0, 0.0, 0.0)],
            messages=[],
            blinks_at={1001.0},
        )
        recording = read_asc(path)
        assert not np.isnan(recording.gaze_x[0])
        assert np.isnan(recording.gaze_x[1])

    def test_an_existing_asc_is_used_rather_than_reconverted(self, tmp_path):
        # Re-converting wastes minutes per session and can silently differ if
        # the other conversion used different flags.
        edf = tmp_path / "run.edf"
        edf.write_bytes(b"not really an edf")
        asc = write_asc(tmp_path / "run.asc", samples=[(1.0, 2.0, 3.0)], messages=[])
        assert ensure_asc(edf) == asc

    def test_a_missing_converter_names_the_developers_kit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        edf = tmp_path / "run.edf"
        edf.write_bytes(b"not really an edf")
        with pytest.raises(DataError, match="Developer's Kit"):
            ensure_asc(edf)


class TestRunReader:
    """`load_run` returns typed frames, not string dicts.

    The footgun this closes: a csv.DictReader row gives `row["success"] ==
    "False"`, and `"False"` is truthy. Every consumer had to remember to
    compare strings, and any that forgot was quietly wrong in the direction
    that looks fine.
    """

    def run_dir(self, tmp_path):
        from alhazen.data.manifest import write_manifest

        run = tmp_path / "sub-t01" / "ses-001" / "run-01_task-demo"
        run.mkdir(parents=True)
        base = "sub-t01_ses-001_run-01_task-demo_20260826"
        (run / f"{base}_trials.csv").write_text(
            "trial_index,attempt,outcome,success,completed,rt_ms\n"
            "1,1,CORRECT,True,True,301.5\n"
            "2,1,BROKE_FIX,False,False,\n"
        )
        (run / f"{base}_events.csv").write_text(
            "trial_index,event,t,payload_json\n"
            '1,TRIAL_START,0.5,"{}"\n'
            '1,STIM_ON,0.75,"{""side"": 1}"\n'
            '2,TRIAL_START,1.5,"{}"\n'
        )
        (run / f"{base}_frames.csv").write_text(
            "trial_index,t,interval_s,dropped\n1,0.5,0.0167,False\n1,0.517,0.0334,True\n"
        )
        (run / "config_snapshot.yaml").write_text("config: {info: {subject: t01}}\n")
        write_manifest(run, run / "manifest.yaml")
        return run

    def test_numbers_come_back_as_numbers(self, tmp_path):
        from alhazen.analysis.io.session import load_run

        run = load_run(self.run_dir(tmp_path))

        assert run.trials["rt_ms"].iloc[0] == pytest.approx(301.5)
        assert run.trials["trial_index"].tolist() == [1, 2]

    def test_booleans_come_back_as_booleans(self, tmp_path):
        from alhazen.analysis.io.session import load_run

        run = load_run(self.run_dir(tmp_path))

        # The whole point: `not row["success"]` now means what it reads as.
        assert run.trials["success"].tolist() == [True, False]
        assert run.trials["completed"].tolist() == [True, False]
        assert run.frames["dropped"].tolist() == [False, True]

    def test_a_missing_value_is_missing_not_the_empty_string(self, tmp_path):
        import pandas as pd

        from alhazen.analysis.io.session import load_run

        run = load_run(self.run_dir(tmp_path))

        assert pd.isna(run.trials["rt_ms"].iloc[1])

    def test_event_times_and_outcome_counts_still_work(self, tmp_path):
        from alhazen.analysis.io.session import load_run

        run = load_run(self.run_dir(tmp_path))

        assert run.event_times("TRIAL_START") == [0.5, 1.5]
        assert run.outcome_counts() == {"CORRECT": 1, "BROKE_FIX": 1}

    def test_payloads_are_decoded_from_the_frame(self, tmp_path):
        from alhazen.analysis.io.session import event_payloads, load_run

        run = load_run(self.run_dir(tmp_path))

        assert event_payloads(run.events, "STIM_ON") == [{"side": 1}]

    def test_a_missing_table_is_an_empty_frame(self, tmp_path):
        from alhazen.analysis.io.session import load_run

        run_dir = self.run_dir(tmp_path)
        next(run_dir.glob("*_frames.csv")).unlink()

        run = load_run(run_dir, verify=False)

        assert run.frames.empty
        assert run.event_times("nothing-here") == []
