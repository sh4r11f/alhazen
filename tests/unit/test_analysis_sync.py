"""Clock alignment: exact recovery, loud refusal, and the stored artifact."""

from __future__ import annotations

import re

import numpy as np
import pytest
import yaml

from alhazen.analysis.sync import (
    MAX_SEED_EVENTS,
    AlignmentFit,
    _seed_window,
    bit_index_for_line,
    event_bit_map,
    fit_alignment,
)
from alhazen.data.percents import compared_percents
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


def varied(
    n: int,
    spacing: float,
    variation: float = 1.0,
    seed: int = 0,
    offset: float = 12.5,
    scale: float = 1.00002,
) -> tuple[np.ndarray, np.ndarray]:
    """Like ``planted``, but trial-to-trial intervals vary by up to ±variation s,
    as a real session's do (a fixation that takes longer, a jittered ITI).

    The variation is what makes an alignment with a missing end pulse
    decidable: on a perfectly regular train a one-trial shift fits exactly as
    well, which is its own test below."""
    rng = np.random.default_rng(seed)
    gaps = spacing + rng.uniform(-variation, variation, n - 1)
    behavior = 1000.0 + np.concatenate([[0.0], np.cumsum(gaps)])
    pulses = offset + scale * (behavior - behavior[0])
    return behavior, pulses


class TestEndPulsesMissing:
    """Issue #36: a pulse train missing the pulse of its first or last event
    was refused as a different session. The seed search anchored the first
    event and the last event, so when either had no pulse every seed tied it
    to another event's pulse — a recording started a few minutes late could
    never be aligned."""

    def assert_planted_map(self, fit: AlignmentFit) -> None:
        # Not just accepted: the map is the planted one, not a map shifted
        # by a trial that happens to explain as many events.
        assert fit.offset_s == pytest.approx(12.5, abs=1e-6)
        assert fit.scale == pytest.approx(1.00002, abs=1e-9)
        assert fit.residual_rms_ms == pytest.approx(0.0, abs=1e-6)

    # The issue's table, one row per case: 6 events ~10 s apart.
    @pytest.mark.parametrize(
        ("case", "keep", "n_matched"),
        [
            ("every pulse present", slice(None), 6),
            ("the middle pulse missing", [0, 1, 2, 4, 5], 5),
            ("the first pulse missing", slice(1, None), 5),
            ("the last pulse missing", slice(None, -1), 5),
        ],
    )
    def test_a_short_train(self, case, keep, n_matched):
        behavior, pulses = varied(6, 10.0)
        fit = fit_alignment("TRIAL_START", behavior, pulses[keep])
        assert fit.n_matched == n_matched, case
        self.assert_planted_map(fit)

    def test_a_short_train_with_an_extra_pulse_after_the_last_event(self):
        behavior, pulses = varied(6, 10.0)
        fit = fit_alignment("TRIAL_START", behavior, np.append(pulses, pulses[-1] + 10.0))
        assert fit.n_matched == 6
        assert fit.n_extra_pulses == 1
        self.assert_planted_map(fit)

    def test_a_long_train_missing_its_first_pulse(self):
        # Was refused: "only 16 of 500 events matched (3% < 80%)".
        behavior, pulses = varied(500, 7.2)
        fit = fit_alignment("TRIAL_START", behavior, pulses[1:])
        assert fit.n_matched == 499
        self.assert_planted_map(fit)

    def test_a_recording_started_five_minutes_late(self):
        # The first 40 of 500 pulses never recorded: 460/500 = 92% matched,
        # above the 80% threshold, so this is an alignment, not a refusal.
        behavior, pulses = varied(500, 7.2)
        fit = fit_alignment("TRIAL_START", behavior, pulses[40:])
        assert fit.n_matched == 460
        assert fit.n_unmatched_behavior == 40
        self.assert_planted_map(fit)

    def test_a_recording_stopped_early(self):
        behavior, pulses = varied(500, 7.2)
        fit = fit_alignment("TRIAL_START", behavior, pulses[:-40])
        assert fit.n_matched == 460
        self.assert_planted_map(fit)

    def test_started_late_and_stopped_early_together(self):
        # 60 missing at the head and 40 at the tail: exactly the 20% the
        # threshold allows, spent across both ends.
        behavior, pulses = varied(500, 7.2)
        fit = fit_alignment("TRIAL_START", behavior, pulses[60:-40])
        assert fit.n_matched == 400
        self.assert_planted_map(fit)

    def test_a_late_start_after_a_test_pulse(self):
        # A check-rig test pulse recorded, then the session's first 40
        # events missed: the anchor is neither the first event nor the
        # first pulse.
        behavior, pulses = varied(500, 7.2)
        train = np.concatenate([[pulses[0] - 500.0], pulses[40:]])
        fit = fit_alignment("TRIAL_START", behavior, train)
        assert fit.n_matched == 460
        assert fit.n_extra_pulses == 1
        self.assert_planted_map(fit)

    def test_the_same_inputs_give_the_same_fit(self):
        behavior, pulses = varied(500, 7.2)
        first = fit_alignment("TRIAL_START", behavior, pulses[40:])
        second = fit_alignment("TRIAL_START", behavior, pulses[40:])
        assert first.to_dict() | {"written": None} == second.to_dict() | {"written": None}


class TestAStrayPulseOneTrialEarly:
    """A stray pulse one trial-gap before the first event, on a train whose
    intervals vary by ±20 ms. The map shifted by one trial (event 0 on the
    stray, event k on event k-1's pulse) puts every event within tolerance
    too, since neighbouring intervals differ by at most 40 ms. The old search
    kept the first seed that explained the most events, and that seed
    anchored event 0 on the stray: the whole map moved by a trial, every
    event still "matched", and the one extra pulse was reported at the far
    end, where nothing looked wrong."""

    @pytest.mark.parametrize("clock_noise_s", [0.0, 1e-4])
    def test_event_0_is_mapped_to_its_own_pulse(self, clock_noise_s):
        behavior, pulses = varied(500, 7.2, variation=0.02)
        pulses = pulses + np.random.default_rng(1).normal(0.0, clock_noise_s, pulses.shape)
        stray = pulses[0] - (pulses[1] - pulses[0])
        fit = fit_alignment("TRIAL_START", behavior, np.concatenate([[stray], pulses]))
        # Both maps explain every event, so only closeness of fit tells them
        # apart: the right one fits to the clock noise, the shift carries
        # the ±20 ms variation.
        assert fit.n_matched == 500
        assert fit.n_extra_pulses == 1
        assert float(fit.to_neural(behavior[0])) == pytest.approx(pulses[0], abs=1e-3)
        assert fit.offset_s == pytest.approx(12.5, abs=1e-3)

    def test_on_a_perfectly_regular_train_it_is_refused(self):
        # With no variation at all the two maps fit equally well, and
        # nothing in the data says which pulse is the stray.
        behavior, pulses = planted(n=500, spacing=7.2)
        stray = pulses[0] - (pulses[1] - pulses[0])
        with pytest.raises(DataError, match="too evenly spaced"):
            fit_alignment("TRIAL_START", behavior, np.concatenate([[stray], pulses]))


class TestStillRefused:
    """Widening the seed search must not let two records of different
    sessions, or a pairing that could be off by whole trials, through."""

    def test_a_different_session_of_the_same_length_and_rate(self):
        # Same trial count, same mean interval, different trial-to-trial
        # variation: a real rig's other session. Seeds find a plausible
        # scale, and then almost nothing matches.
        behavior, _ = varied(500, 7.2, seed=0)
        _, other = varied(500, 7.2, seed=9)
        with pytest.raises(DataError, match="confidently wrong"):
            fit_alignment("TRIAL_START", behavior, other)

    def test_a_late_start_beyond_the_threshold_says_how_far_it_looked(self):
        # 101 of 500 missing: even the right map would match 79.8%, under
        # 80%. The message names the window, so a late start is recognisable.
        behavior, pulses = varied(500, 7.2)
        with pytest.raises(DataError, match="compared the first and last 101 events") as info:
            fit_alignment("TRIAL_START", behavior, pulses[101:])
        assert "confidently wrong" in str(info.value)

    def test_a_late_start_beyond_the_cap_is_refused_loudly(self, monkeypatch):
        # The window is capped whatever the threshold allows. A late start
        # past the cap cannot be seeded, and says so rather than guessing.
        monkeypatch.setattr("alhazen.analysis.sync.MAX_SEED_EVENTS", 10)
        behavior, pulses = varied(500, 7.2)
        with pytest.raises(DataError, match="compared the first and last 10 events"):
            fit_alignment("TRIAL_START", behavior, pulses[40:])

    def test_the_scale_refusal_says_what_was_compared(self):
        behavior, _ = planted(n=20)
        unrelated = np.arange(20) * 0.31 + 5.0
        with pytest.raises(DataError, match="compared the first and last 5 events with the first"):
            fit_alignment("TRIAL_START", behavior, unrelated)

    @pytest.mark.parametrize("keep", [slice(1, None), slice(None, -1)])
    def test_a_perfectly_regular_short_train_missing_an_end_pulse(self, keep):
        # Five pulses 10 s apart for six events 10 s apart: pulses 0-4 are
        # events 1-5 or events 0-4, and nothing in the data says which.
        # Returning either would be off by a whole trial half the time.
        behavior, pulses = planted(n=6, spacing=10.0)
        with pytest.raises(DataError, match="too evenly spaced"):
            fit_alignment("TRIAL_START", behavior, pulses[keep])

    def test_a_perfectly_regular_late_start_names_the_shift(self):
        behavior, pulses = planted(n=500, spacing=7.2)
        with pytest.raises(DataError, match="too evenly spaced") as info:
            fit_alignment("TRIAL_START", behavior, pulses[40:])
        # The competing maps differ by whole trials — the number says so.
        # (On a regular train a late start of 40 and an early stop of 40
        # are the same picture, so the rival may be that far off.)
        found = re.search(r"up to ([\d.]+) s apart", str(info.value))
        assert found is not None
        trials = float(found.group(1)) / 7.2
        assert trials >= 1
        assert trials == pytest.approx(round(trials), abs=1e-3)

    def test_clock_noise_does_not_hide_the_ambiguity(self):
        # Real pulses carry timing noise, so the two fits are no longer both
        # exact; on a regular schedule they are still equally good.
        behavior, pulses = planted(n=500, spacing=7.2)
        noisy = pulses + np.random.default_rng(0).normal(0.0, 1e-4, pulses.shape)
        with pytest.raises(DataError, match="too evenly spaced"):
            fit_alignment("TRIAL_START", behavior, noisy[40:])

    def test_a_little_timing_variation_is_enough_to_decide(self):
        # ±20 ms of trial-to-trial variation against 0.1 ms of clock noise:
        # a one-trial shift fits two orders of magnitude worse, so the
        # right map is chosen, not refused.
        behavior, pulses = varied(500, 7.2, variation=0.02)
        noisy = pulses + np.random.default_rng(0).normal(0.0, 1e-4, pulses.shape)
        fit = fit_alignment("TRIAL_START", behavior, noisy[40:])
        assert fit.n_matched == 460
        assert fit.offset_s == pytest.approx(12.5, abs=1e-3)

    def test_spurious_edges_near_the_pulses_are_not_a_rival_map(self):
        # Jittered pulses with as many random edges again: near-copies of
        # the right map pick different spurious edges within tolerance and
        # tie on count. They put every event in the same place to within
        # the tolerance, so they are one map, not an ambiguity.
        behavior, pulses = planted(n=60)
        rng = np.random.default_rng(0)
        jittered = pulses + rng.normal(0.0, 0.03, pulses.shape)
        extras = rng.uniform(jittered.min(), jittered.max(), 60)
        fit = fit_alignment("TRIAL_START", behavior, np.concatenate([jittered, extras]))
        assert fit.offset_s == pytest.approx(12.5, abs=0.05)

    def test_a_complete_regular_train_is_not_ambiguous(self):
        # With every pulse present a shift loses an event, so the right map
        # explains strictly more and no ambiguity is raised.
        behavior, pulses = planted(n=500, spacing=7.2)
        assert fit_alignment("TRIAL_START", behavior, pulses).n_matched == 500


class TestSeedWindow:
    """How many events at each end the seed search tries: as many as the
    matched threshold lets go unmatched, plus one, within a hard cap."""

    def test_it_is_the_unmatched_budget_plus_one(self):
        # 20% of 500 may go unmatched: events 0-100 can each be the first
        # with a pulse.
        assert _seed_window(500, 0.8) == 101
        assert _seed_window(6, 0.8) == 2

    def test_floating_point_does_not_cost_an_event(self):
        # 0.55 × 100 is 55.00000000000001 in floating point; 45 events may
        # still go unmatched, not 44.
        assert _seed_window(100, 0.55) == 46

    def test_a_strict_threshold_anchors_on_the_ends_only(self):
        assert _seed_window(500, 1.0) == 1

    def test_a_permissive_threshold_is_capped(self):
        assert _seed_window(100_000, 0.0) == MAX_SEED_EVENTS
        assert _seed_window(10, 0.0) == 10


class TestRefusalPercentages:
    """The matched-fraction refusal once rounded both numbers to whole
    percents, so 399 of 500 against 80% read "(80% < 80%)" — a refusal that
    seemed to contradict itself. The fraction now carries as many decimals as
    it takes to be visibly below the threshold.

    The rule has since moved to alhazen.data.percents, shared with frame QA
    and the runner. These are the cases it had here, called the way the
    refusal calls it ("<", whole percents allowed), with the same expected
    text: moving it changed nothing the alignment prints."""

    def test_399_of_500_reads_as_below_80_percent(self):
        # 101 pulses lost mid-session: the ends anchor the right map, and it
        # matches 399 of 500 — 79.8%, a fifth of a percent short.
        behavior, pulses = varied(500, 7.2)
        with pytest.raises(DataError, match=r"only 399 of 500 .*\(79\.8% < 80%\)"):
            fit_alignment("TRIAL_START", behavior, np.delete(pulses, np.arange(200, 301)))

    @pytest.mark.parametrize(
        ("fraction", "threshold", "shown"),
        [
            # The boundary case, one decimal.
            (399 / 500, 0.8, ("79.8%", "80%")),
            # A clear miss keeps whole percents.
            (16 / 500, 0.8, ("3%", "80%")),
            # One decimal rounds UP to "80.0", so it takes two.
            (0.7995, 0.8, ("79.95%", "80%")),
            # A threshold with a decimal of its own is written with it.
            (0.85, 0.855, ("85%", "85.5%")),
            # 0.55 × 100 is 55.00000000000001; the threshold still reads 55%.
            (0.5499, 0.55, ("54.99%", "55%")),
            (99_999 / 100_000, 1.0, ("99.999%", "100%")),
        ],
    )
    def test_the_fraction_is_written_visibly_below_the_threshold(self, fraction, threshold, shown):
        assert compared_percents(fraction, "<", threshold) == shown

    def test_a_threshold_a_rounding_error_away_falls_back_to_full_precision(self):
        # 3 of 10 against 0.1 + 0.2 = 0.30000000000000004: refused, and no
        # number of decimals separates them as percents. Full precision does.
        assert compared_percents(0.3, "<", 0.1 + 0.2) == ("0.3", "0.30000000000000004")


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


class TestSavingKeepsTheRunsDamageVisible:
    """`AlignmentFit.save` re-hashed the whole run after writing its file,
    recording anything damaged since the session as the session's own."""

    def test_only_the_alignment_is_added_to_the_manifest(self, tmp_path):
        from alhazen.data.manifest import verify_manifest, write_manifest

        (tmp_path / "trials.csv").write_text("a\n1\n")
        manifest_path = tmp_path / "manifest.yaml"
        write_manifest(tmp_path, manifest_path)
        (tmp_path / "trials.csv").write_text("a\n2\n")  # changed after the session

        behavior, pulses = planted()
        fit_alignment("TRIAL_START", behavior, pulses).save(tmp_path)

        assert verify_manifest(tmp_path, manifest_path) == ["hash mismatch: trials.csv"]
