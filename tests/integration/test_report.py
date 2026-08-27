"""Acceptance: a simulated session plus synthetic neural files produce an
alignment artifact and a report with the planted display latency
recovered."""

from __future__ import annotations

import numpy as np
import pytest
import yaml

from alhazen import Duration, build_session
from alhazen.analysis.io.session import load_run
from alhazen.analysis.photodiode import find_edges, measure_latency
from alhazen.analysis.report import build_report
from alhazen.analysis.sync import fit_alignment
from alhazen.cli.main import main
from alhazen.config.models import (
    DevicesConfig,
    DisplayConfig,
    PhotodiodeConfig,
    RecordingConfig,
    RigConfig,
    SyncHwConfig,
)
from alhazen.core.events import EventSchema
from alhazen.errors import DataError
from alhazen.paradigms.base import Condition, SimpleSequence
from alhazen.task.plan import TrialPlan
from fixtures_neural import write_nidq
from support import COMPLETED, MONITOR, RunForFrames

# The line map the session runs with, and that the analysis must read back
# out of the run's own snapshot rather than being told.
EVENT_LINES = {"TRIAL_START": "Dev1/port0/line0", "STIM_ON": "Dev1/port0/line1"}
# What the display actually does, and what the report must recover.
PLANTED_LATENCY_S = 0.012


def run_session(tmp_path, n_trials: int = 8):
    """A simulated session with sync mapped and a photodiode armed."""
    rig = RigConfig(
        monitor=MONITOR,
        display=DisplayConfig(
            backend="simulated",
            photodiode=PhotodiodeConfig(events=["STIM_ON"]),
        ),
        devices=DevicesConfig(
            sync=SyncHwConfig(backend="simulated", event_lines=EVENT_LINES),
            recording=RecordingConfig(backend="simulated"),
        ),
        data_root=tmp_path,
    )
    runner = build_session(
        rig=rig,
        subject="t01",
        session=1,
        run=1,
        task_name="report-demo",
        task_params=Duration(ms=1),  # any pydantic model; nothing reads it here
        event_schema=EventSchema(("STIM_ON",)),
        build_trial=lambda setup: TrialPlan(
            phases=[RunForFrames(2, COMPLETED, emit_on_enter="STIM_ON")]
        ),
        make_source=lambda params, rng: SimpleSequence(
            [Condition({"c": "a"})], n_repeats=n_trials, rng=rng
        ),
        seed=3,
        # A real gap between trials. Two things need it: pulses must be
        # further apart than they are wide, or they merge into one long
        # high; and marked events must be further apart than the display's
        # latency, or "the first edge after this event" pairs an edge with
        # the wrong event. Both hold comfortably on a real rig, where trials
        # are seconds apart and latency is milliseconds.
        iti=Duration(ms=50),
        simulated_frame_period_s=0.0,
        date_yyyymmdd="20260826",
    )
    runner.run()
    return next(tmp_path.glob("sub-t01/ses-001/run-*"))


def write_matching_recording(tmp_path, run_dir, latency_s: float = PLANTED_LATENCY_S):
    """Synthetic neural files that recorded this very session.

    Pulse times are the session's own event times, mapped through a known
    clock offset and drift — so the alignment has a right answer to find.
    """
    run = load_run(run_dir)
    offset, scale = 4.25, 1.00003  # 30 ppm apart, as two crystals are
    starts = np.asarray(run.event_times("TRIAL_START"))
    stims = np.asarray(run.event_times("STIM_ON"))
    t0 = starts[0]

    def to_recording(times):
        return offset + scale * (times - t0)

    duration = float(to_recording(np.array([max(starts.max(), stims.max())]))[0]) + 5.0
    return write_nidq(
        tmp_path / "neural_g0",
        pulses={0: list(to_recording(starts)), 1: list(to_recording(stims))},
        duration_s=duration,
        rate_hz=25000.0,
        # Narrow pulses and a brief flash, so neither runs into the next
        # trial's.
        pulse_width_s=0.0005,
        analog_high_s=0.001,
        # The screen changes a little after the software said so; that delay
        # is what the photodiode measures.
        analog_edges=list(to_recording(stims) + latency_s),
    )


class TestSessionOnly:
    def test_the_report_describes_the_session(self, tmp_path):
        run_dir = run_session(tmp_path)
        report = build_report(run_dir)
        assert report.identity["subject"] == "t01"
        assert report.trials["n_rows"] == 8
        assert report.trials["outcomes"]["COMPLETED"] == 8
        assert report.frames["n_frames"] > 0
        assert report.manifest_problems == []
        assert report.ok

    def test_a_tampered_run_fails_its_manifest(self, tmp_path):
        run_dir = run_session(tmp_path)
        trials = next(run_dir.glob("*_trials.csv"))
        with trials.open("a") as handle:
            handle.write("999,1,COMPLETED,True,a\n")  # an edit nobody recorded
        report = build_report(run_dir)
        assert not report.ok
        assert any("trials" in problem for problem in report.manifest_problems)

    def test_the_recording_pointer_is_written_and_hashed(self, tmp_path):
        run_dir = run_session(tmp_path)
        pointer = run_dir / "recording_pointer.yaml"
        assert pointer.exists()
        contents = yaml.safe_load(pointer.read_text())
        assert contents["subject"] == "t01"
        # And the manifest covers it, so a run and its pointer travel together.
        manifest = yaml.safe_load((run_dir / "manifest.yaml").read_text())
        assert any(item["path"] == "recording_pointer.yaml" for item in manifest["artifacts"])


class TestTheReportDoesNotPoisonTheRun:
    """Spec 6.2 requires the manifest to be re-written to cover the artifacts
    the report itself writes. Without it, the first ``alhazen report`` leaves
    two unlisted files behind and every later report — and every ``load_run``
    — on that run comes back with a failed manifest."""

    def manifest_paths(self, run_dir):
        manifest = yaml.safe_load((run_dir / "manifest.yaml").read_text())
        return {item["path"] for item in manifest["artifacts"]}

    def test_a_second_report_still_verifies(self, tmp_path):
        run_dir = run_session(tmp_path)

        build_report(run_dir).save()
        second = build_report(run_dir)
        second.save()

        assert second.manifest_problems == []
        assert second.ok
        assert "report.yaml" in self.manifest_paths(run_dir)

    def test_a_third_report_still_verifies(self, tmp_path):
        # The rewrite has to be idempotent: report N covers report N-1's file,
        # and its own rewrite covers the file it just wrote.
        run_dir = run_session(tmp_path)
        for _ in range(3):
            report = build_report(run_dir)
            report.save()
            assert report.manifest_problems == []

    def test_load_run_after_a_report_is_clean(self, tmp_path):
        run_dir = run_session(tmp_path)
        build_report(run_dir).save()

        assert load_run(run_dir).manifest_problems == []

    def test_the_alignment_artifact_is_listed_too(self, tmp_path):
        run_dir = run_session(tmp_path)
        write_matching_recording(tmp_path, run_dir)
        neural = tmp_path / "neural_g0"

        build_report(run_dir, neural).save()
        second = build_report(run_dir, neural)
        second.save()

        listed = self.manifest_paths(run_dir)
        assert "alignment_spikeglx.yaml" in listed
        assert "report.yaml" in listed
        assert second.manifest_problems == []
        assert second.ok


class TestWithARecording:
    def test_alignment_recovers_the_planted_clock_map(self, tmp_path):
        run_dir = run_session(tmp_path)
        write_matching_recording(tmp_path, run_dir)
        report = build_report(run_dir, tmp_path / "neural_g0")

        assert report.alignment is not None
        fit = report.alignment
        assert fit.n_matched == fit.n_behavior
        assert fit.residual_rms_ms < 0.1
        # The map itself is what matters here: every event lands on its pulse
        # to within a sample period. The *rate* (ppm) is deliberately not
        # asserted on this fixture — a session spanning under a second cannot
        # resolve tens of ppm against a 40 us sample grid, and pretending
        # otherwise would make this test a coin flip. Drift recovery is
        # tested where it can be: on a minutes-long synthetic train, in
        # tests/unit/test_analysis_sync.py.
        # EVERY event, not the average of them: `residual_rms_ms` above would
        # forgive one event landing on the wrong pulse if the rest were exact.
        # (This used to assert that the predicted times were evenly *spaced*,
        # which is a claim about how uniformly the fixture's session happened
        # to run — false on a loaded machine, and never about the alignment.)
        assert fit.residual_max_ms < 0.1
        # And the artifact is on disk beside the run.
        stored = yaml.safe_load((run_dir / "alignment_spikeglx.yaml").read_text())
        assert stored["event"] in EVENT_LINES

    def test_the_planted_display_latency_is_recovered(self, tmp_path):
        # This closes the loop: the patch marked the flip, the diode
        # saw it, and the difference is the rig's real display latency.
        run_dir = run_session(tmp_path)
        write_matching_recording(tmp_path, run_dir, latency_s=0.012)
        report = build_report(run_dir, tmp_path / "neural_g0")

        assert report.photodiode is not None
        assert report.photodiode.median_latency_ms == pytest.approx(12.0, abs=1.0)
        assert report.photodiode.n_matched > 0

    def test_the_line_map_comes_from_the_runs_own_snapshot(self, tmp_path):
        # The rule that the worst alignment bugs come from breaking: an
        # analysis that re-declares the channel map will one day be wrong
        # about a session it was not written for.
        run_dir = run_session(tmp_path)
        run = load_run(run_dir)
        assert run.sync_event_lines == EVENT_LINES
        assert run.photodiode_events == ["STIM_ON"]

    def test_every_configured_line_is_counted_not_only_the_fitted_one(self, tmp_path):
        """The alignment fits on one line. A second line that was wired but
        never pulsed — a loose BNC — is invisible in a report that describes
        only the line it fitted."""
        run_dir = run_session(tmp_path)
        write_matching_recording(tmp_path, run_dir)

        report = build_report(run_dir, tmp_path / "neural_g0")

        assert set(report.sync_lines) == set(EVENT_LINES)
        for event, counts in report.sync_lines.items():
            assert counts["line"] == EVENT_LINES[event]
            assert counts["n_events"] == 8
            assert counts["n_pulses"] == 8
        assert "sync TRIAL_START" in report.render()

    def test_a_line_with_no_pulses_shows_up_as_a_zero(self, tmp_path):
        run_dir = run_session(tmp_path)
        run = load_run(run_dir)
        offset, scale = 4.25, 1.00003
        starts = np.asarray(run.event_times("TRIAL_START"))
        t0 = starts[0]
        recorded = offset + scale * (starts - t0)
        # Line 1 (STIM_ON) is wired in the rig config but nothing reached it.
        write_nidq(
            tmp_path / "half_g0",
            pulses={0: list(recorded)},
            duration_s=float(recorded.max()) + 5.0,
            rate_hz=25000.0,
            pulse_width_s=0.0005,
        )

        report = build_report(run_dir, tmp_path / "half_g0")

        assert report.sync_lines["STIM_ON"]["n_events"] == 8
        assert report.sync_lines["STIM_ON"]["n_pulses"] == 0

    def test_a_recording_of_a_different_session_is_refused(self, tmp_path):
        run_dir = run_session(tmp_path)
        # Pulses that have nothing to do with this session.
        write_nidq(
            tmp_path / "neural_g0",
            pulses={0: list(np.arange(10) * 0.37 + 1.0)},
            duration_s=10.0,
        )
        report = build_report(run_dir, tmp_path / "neural_g0")
        assert report.alignment is None
        assert any("alignment refused" in problem for problem in report.problems)
        assert not report.ok


class TestPhotodiodeMaths:
    def test_edges_are_found_at_the_planted_times(self):
        rate = 1000.0
        trace = np.zeros(5000)
        for start in (500, 1500, 2500):
            trace[start : start + 50] = 1.0
        assert np.allclose(find_edges(trace, rate), [0.5, 1.5, 2.5], atol=1e-3)

    def test_a_flat_trace_says_so(self):
        with pytest.raises(DataError, match="never changes"):
            find_edges(np.zeros(100), 1000.0)

    def test_an_edge_before_its_event_is_never_matched_backwards(self):
        # The screen cannot change before the flip that changed it; allowing
        # it would let a stray edge produce a negative latency that looks
        # like a clock error.
        behavior = np.array([1.0, 2.0, 3.0, 4.0])
        fit = fit_alignment("STIM_ON", behavior, behavior)
        edges = behavior + 0.02
        report = measure_latency(behavior, np.concatenate([[0.1], edges]), fit)
        assert report.median_latency_ms == pytest.approx(20.0, abs=0.1)


class TestReportCli:
    def test_the_cli_prints_and_writes_the_report(self, tmp_path, capsys):
        run_dir = run_session(tmp_path)
        assert main(["report", "--run", str(run_dir)]) == 0
        out = capsys.readouterr().out
        assert "trials:" in out and "manifest: verified" in out
        assert (run_dir / "report.yaml").exists()

    def test_a_failed_manifest_exits_nonzero(self, tmp_path, capsys):
        run_dir = run_session(tmp_path)
        (run_dir / "session.log").write_text("edited after the fact")
        assert main(["report", "--run", str(run_dir)]) == 1
        assert "manifest: FAILED" in capsys.readouterr().out

    def test_an_unreadable_run_says_so_rather_than_reporting(self, tmp_path, capsys):
        assert main(["report", "--run", str(tmp_path)]) == 1
        assert "CANNOT READ RUN" in capsys.readouterr().err
