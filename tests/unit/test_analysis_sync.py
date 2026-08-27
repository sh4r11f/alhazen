"""Clock alignment: exact recovery, loud refusal, and the stored artifact."""

from __future__ import annotations

import numpy as np
import pytest
import yaml

from alhazen.analysis.sync import (
    AlignmentFit,
    bit_index_for_line,
    event_bit_map,
    fit_alignment,
)
from alhazen.errors import DataError


def planted(
    n: int = 40,
    offset: float = 12.5,
    scale: float = 1.00002,  # 20 ppm: two ordinary crystals
    spacing: float = 1.7,
) -> tuple[np.ndarray, np.ndarray]:
    """A behavioral train and its recording of itself, with a known map."""
    behavior = 1000.0 + np.arange(n) * spacing
    pulses = offset + scale * (behavior - behavior[0])
    return behavior, pulses


class TestFitting:
    def test_a_known_map_is_recovered_exactly(self):
        behavior, pulses = planted()
        fit = fit_alignment("TRIAL_START", behavior, pulses)
        assert fit.n_matched == len(behavior)
        assert fit.residual_rms_ms == pytest.approx(0.0, abs=1e-6)
        # And the map it fitted maps: round-tripping a time returns it.
        assert np.allclose(fit.to_behavior(fit.to_neural(behavior)), behavior)

    def test_drift_is_reported_in_ppm(self):
        behavior, pulses = planted(scale=1.00002)
        fit = fit_alignment("TRIAL_START", behavior, pulses)
        assert fit.drift_ppm == pytest.approx(20.0, abs=1.0)

    def test_extra_pulses_are_counted_not_absorbed(self):
        # A test pulse before the session, and a stray one after: neither
        # should shift the fit, and both should be reported.
        behavior, pulses = planted()
        with_extras = np.concatenate([[0.5], pulses, [pulses[-1] + 30.0]])
        fit = fit_alignment("TRIAL_START", behavior, with_extras)
        assert fit.n_matched == len(behavior)
        assert fit.n_extra_pulses == 2
        assert fit.residual_rms_ms == pytest.approx(0.0, abs=1e-6)

    def test_dropped_pulses_are_counted_not_hidden(self):
        behavior, pulses = planted(n=40)
        # Three pulses never made it to the recording.
        kept = np.delete(pulses, [5, 17, 30])
        fit = fit_alignment("TRIAL_START", behavior, kept)
        assert fit.n_unmatched_behavior == 3
        assert fit.n_matched == 37

    def test_too_few_matches_is_refused(self):
        # Pulses that span the same time and start and end in the right
        # places, but wander in between — so a scale is found and then almost
        # nothing matches. Two records of different sessions look like this,
        # and a transform fitted from them would be confidently wrong.
        behavior, pulses = planted(n=20)
        wandering = pulses.copy()
        wandering[1:-1] += 0.4 * ((-1.0) ** np.arange(len(pulses) - 2))
        with pytest.raises(DataError, match="confidently wrong"):
            fit_alignment("TRIAL_START", behavior, wandering)

    def test_pulses_at_the_wrong_rate_are_refused_before_fitting(self):
        # The other refusal: no pairing of endpoints gives a clock scale
        # anywhere near 1, so there is nothing to refine.
        behavior, _ = planted(n=20)
        unrelated = np.arange(20) * 0.31 + 5.0
        with pytest.raises(DataError, match="do not appear to be the same one"):
            fit_alignment("TRIAL_START", behavior, unrelated)

    def test_too_few_events_to_fit_at_all(self):
        with pytest.raises(DataError, match="at least 3"):
            fit_alignment("TRIAL_START", [1.0, 2.0], [1.0, 2.0, 3.0])

    def test_a_clock_that_disagrees_wildly_is_a_wrong_pairing(self):
        behavior, _ = planted(n=20)
        # Pulses at half the rate: no plausible clock scale explains this.
        with pytest.raises(DataError):
            fit_alignment("TRIAL_START", behavior, (behavior - behavior[0]) * 0.5)


class TestLineMap:
    def test_a_line_string_resolves_to_its_bit(self):
        assert bit_index_for_line("Dev1/port0/line5") == 5

    def test_an_unreadable_line_says_where_it_came_from(self):
        with pytest.raises(DataError, match="sync.event_lines"):
            bit_index_for_line("the third one")

    def test_two_events_on_one_line_is_an_error(self):
        # Their pulses would be indistinguishable in the recording.
        with pytest.raises(DataError, match="indistinguishable"):
            event_bit_map({"A": "Dev1/port0/line0", "B": "Dev1/port0/line0"})

    def test_a_bit_outside_the_word_is_refused(self):
        with pytest.raises(DataError, match="16-bit"):
            bit_index_for_line("Dev1/port0/line99")


class TestArtifact:
    def test_the_fit_round_trips_through_its_file(self, tmp_path):
        # An alignment recomputed next year with a different tolerance is a
        # different alignment; the one that was used has to be on disk.
        behavior, pulses = planted()
        fit = fit_alignment("TRIAL_START", behavior, pulses)
        path = fit.save(tmp_path)
        stored = yaml.safe_load(path.read_text())
        assert stored["event"] == "TRIAL_START"
        assert stored["n_matched"] == fit.n_matched
        assert stored["offset_s"] == pytest.approx(fit.offset_s)
        # Enough to reconstruct the map without the original object.
        rebuilt = AlignmentFit(
            event=stored["event"],
            offset_s=stored["offset_s"],
            scale=stored["scale"],
            t0_behavior_s=stored["t0_behavior_s"],
            n_behavior=stored["n_behavior"],
            n_pulses=stored["n_pulses"],
            n_matched=stored["n_matched"],
            residual_rms_ms=stored["residual_rms_ms"],
            residual_max_ms=stored["residual_max_ms"],
        )
        assert np.allclose(rebuilt.to_neural(behavior), fit.to_neural(behavior))

    def test_the_filename_names_the_system(self, tmp_path):
        behavior, pulses = planted()
        path = fit_alignment("TRIAL_START", behavior, pulses).save(tmp_path, system="openephys")
        assert path.name == "alignment_openephys.yaml"


class TestLineNumberAnchoring:
    """The pattern is anchored to the end of the string. Without the anchor
    any earlier "line<digits>" wins — and a rig whose device path contains
    one silently pulses a different bit than it was told to."""

    def test_a_trailing_line_number_wins_over_an_earlier_one(self):
        assert bit_index_for_line("C:/rigs/baseline5/Dev1/port0/line3") == 3

    def test_whitespace_around_the_number_is_tolerated(self):
        assert bit_index_for_line("Dev1/port0/line 7") == 7

    def test_case_does_not_matter(self):
        assert bit_index_for_line("Dev1/Port0/Line2") == 2

    def test_a_path_with_no_trailing_line_number_is_refused(self):
        # Unanchored, "baseline5/port0" resolved to bit 5 — a confident answer
        # about a wire nobody chose. Anchored, there is nothing to read and
        # the error says where the string came from.
        with pytest.raises(DataError, match="cannot read a line number"):
            bit_index_for_line("Dev1/baseline5/port0")


class TestFinalRefit:
    """The refine loop stops after a fixed number of rounds. When it stops on
    that cap rather than on convergence, the match set it returns is one round
    newer than the offset and scale fitted from it — so the residual statistics
    described a map that was not the map being returned.

    The cap is a module constant precisely so a test can force it; a converged
    fit cannot see this seam, which is why nothing did."""

    def noisy_train(self):
        """A jittered train with enough spurious edges that the first refine
        round genuinely moves the match set — otherwise the loop converges on
        round one and the cap is never reached."""
        behavior, pulses = planted(n=60)
        rng = np.random.default_rng(0)
        jittered = pulses + rng.normal(0.0, 0.03, pulses.shape)
        extras = rng.uniform(jittered.min(), jittered.max(), 60)
        return behavior, np.sort(np.concatenate([jittered, extras]))

    def test_the_map_is_the_least_squares_fit_of_what_it_returns(self, monkeypatch):
        # One refine round: the loop can only exit on the cap.
        monkeypatch.setattr("alhazen.analysis.sync.MAX_REFINE_ITERATIONS", 1)
        behavior, train = self.noisy_train()

        fit = fit_alignment("TRIAL_START", behavior, train)

        # A least-squares fit with an intercept leaves residuals summing to
        # zero over the points it was fitted on. A stale fit does not.
        assert float(np.mean(fit.residuals_ms)) == pytest.approx(0.0, abs=1e-6)
        assert np.asarray(fit.residuals_ms).size == fit.n_matched

    def test_the_reported_statistics_match_the_reported_residuals(self, monkeypatch):
        monkeypatch.setattr("alhazen.analysis.sync.MAX_REFINE_ITERATIONS", 1)
        behavior, train = self.noisy_train()

        fit = fit_alignment("TRIAL_START", behavior, train)

        residuals = np.asarray(fit.residuals_ms)
        assert float(np.sqrt(np.mean(residuals**2))) == pytest.approx(fit.residual_rms_ms)
        assert float(np.max(np.abs(residuals))) == pytest.approx(fit.residual_max_ms)

    def test_a_clean_train_is_unaffected(self):
        behavior, pulses = planted()
        fit = fit_alignment("TRIAL_START", behavior, pulses)
        assert fit.residual_rms_ms == pytest.approx(0.0, abs=1e-6)
