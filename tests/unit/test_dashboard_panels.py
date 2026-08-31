"""What each dashboard panel plots.

Every statistic the dashboard shows is computed in Python precisely so it can
be tested here: a running accuracy with the wrong denominator, a cumulative
curve that restarts mid-session, or a histogram whose bins do not add up to n
all look perfectly plausible in a browser.
"""

from __future__ import annotations

import json
import math

import pytest

from alhazen.dashboard.panels import (
    MAX_CLASSES,
    MAX_POINTS,
    axis_label,
    format_number,
    panel_payload,
    select_rows,
    split_unit,
)
from alhazen.dashboard.spec import DEFAULT_PANELS, DashboardPanel


def trial(index: int, **fields):
    return {"trial_index": index, **fields}


def reward_event(index: int, *, n_pulses: int = 2, pulse_ms: int = 200, manual: bool = False):
    return {
        "trial_index": index,
        "event": "REWARD",
        "t": float(index),
        "payload_json": json.dumps(
            {"manual": manual, "pulses": {"n_pulses": n_pulses, "pulse_ms": pulse_ms}}
        ),
    }


def payload(panel: DashboardPanel, trials=(), events=()):
    return panel_payload(panel, list(trials), list(events))


class TestLabels:
    """Column names in this framework carry their unit; an axis without one
    is not a scientific axis."""

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("rt_ms", "rt (ms)"),
            ("endpoint_x_dva", "endpoint x (dva)"),
            ("response_key", "response key"),
            ("coherence", "coherence"),
        ],
    )
    def test_units_are_read_off_the_column_name(self, field, expected):
        assert axis_label(field) == expected

    def test_an_explicit_unit_overrides_the_suffix(self):
        assert axis_label("rt_ms", unit="s") == "rt (s)"
        assert split_unit("gain") == ("gain", None)

    def test_numbers_carry_the_decimals_they_deserve(self):
        assert format_number(12345.6) == "12,346"
        assert format_number(12.34) == "12.3"
        assert format_number(0.5) == "0.5"
        assert format_number(math.nan) == "—"


class TestRowSelection:
    def test_completed_only_reads_the_rows_completed_column(self):
        # Not `success is not None`: an incomplete trial can still record one.
        rows = [
            trial(1, completed=True, success=False),
            trial(2, completed=False, success=True),
            trial(3, completed=True),
        ]
        panel = DashboardPanel(kind="outcomes", title="x", completed_only=True)
        assert [row["trial_index"] for row in select_rows(panel, rows)] == [1, 3]

    def test_rolling_window_keeps_the_most_recent(self):
        rows = [trial(i) for i in range(1, 11)]
        panel = DashboardPanel(kind="outcomes", title="x", rolling_window=3)
        assert [row["trial_index"] for row in select_rows(panel, rows)] == [8, 9, 10]

    def test_the_window_is_announced_on_the_panel(self):
        rows = [trial(i, outcome="CORRECT") for i in range(1, 11)]
        panel = DashboardPanel(kind="outcomes", title="x", rolling_window=4)
        assert payload(panel, rows)["note"] == "most recent 4 trials"


class TestCategoryBars:
    def test_counts_shares_and_ordering(self):
        rows = [trial(i, outcome="CORRECT") for i in range(8)]
        rows += [trial(i, outcome="FIX_BREAK") for i in range(2)]
        data = payload(DashboardPanel(kind="outcomes", title="Outcomes"), rows)

        assert data["form"] == "bars"
        assert data["total"] == 10
        assert [item["label"] for item in data["items"]] == ["CORRECT", "FIX_BREAK"]
        assert data["items"][0]["value"] == 8
        assert data["items"][0]["share"] == pytest.approx(0.8)

    def test_a_long_tail_folds_rather_than_growing_unreadable(self):
        rows = [trial(i, outcome=f"O{i:02d}") for i in range(MAX_CLASSES + 4)]
        data = payload(DashboardPanel(kind="outcomes", title="Outcomes"), rows)

        assert len(data["items"]) == MAX_CLASSES + 1
        assert data["items"][-1]["label"] == "Other (4 more)"
        assert sum(item["value"] for item in data["items"]) == data["total"]

    def test_missing_column_says_so_instead_of_drawing_nothing(self):
        data = payload(DashboardPanel(kind="responses", title="Responses", value="response_key"))
        assert data == {"form": "empty", "message": "No response_key recorded yet"}


class TestHistogram:
    def test_bins_tile_the_range_and_account_for_every_trial(self):
        rows = [trial(i, rt_ms=200 + 7 * i) for i in range(60)]
        data = payload(DashboardPanel(kind="histogram", title="RT", value="rt_ms"), rows)

        assert data["form"] == "histogram"
        assert sum(b["count"] for b in data["bins"]) == 60
        assert 4 <= len(data["bins"]) <= 30
        edges = [b["x0"] for b in data["bins"]] + [data["bins"][-1]["x1"]]
        assert edges == sorted(edges)
        assert data["x_label"] == "rt (ms)"

    def test_one_outlier_does_not_squeeze_every_real_trial_into_one_bar(self):
        # The axis is clipped to a robust window, so a single 8-second trial
        # cannot collapse the other sixty into one spike — and the excluded
        # trial is stated, never quietly dropped.
        rows = [trial(i, rt_ms=300 + i) for i in range(60)] + [trial(99, rt_ms=8000)]
        data = payload(DashboardPanel(kind="histogram", title="RT", value="rt_ms"), rows)

        # Without the robust window the axis would run to 8000 ms and every
        # real trial would land in the first bar.
        occupied = [b for b in data["bins"] if b["count"]]
        assert len(occupied) >= 3
        assert data["bins"][-1]["x1"] < 1000
        assert data["note"] == "1 trial outside the axis (full range 300–8,000 ms)"
        # n and the median still describe every trial.
        assert {stat["label"]: stat["value"] for stat in data["stats"]}["n"] == "61"

    def test_the_window_note_never_replaces_what_the_panel_already_said(self):
        rows = [trial(i, rt_ms=300 + i) for i in range(60)] + [trial(99, rt_ms=8000)]
        panel = DashboardPanel(kind="histogram", title="RT", value="rt_ms", rolling_window=61)
        note = payload(panel, rows)["note"]
        assert note.startswith("most recent 61 trials · ")
        assert "outside the axis" in note

    def test_identical_values_produce_one_bin_not_a_division_by_zero(self):
        rows = [trial(i, rt_ms=250) for i in range(5)]
        data = payload(DashboardPanel(kind="histogram", title="RT", value="rt_ms"), rows)

        assert len(data["bins"]) == 1
        assert data["bins"][0]["count"] == 5
        assert data["median"] == 250

    def test_the_median_is_reported_with_its_unit(self):
        rows = [trial(i, rt_ms=v) for i, v in enumerate([100, 200, 300])]
        data = payload(DashboardPanel(kind="histogram", title="RT", value="rt_ms"), rows)
        stats = {stat["label"]: stat["value"] for stat in data["stats"]}
        assert stats["median"] == "200 ms"
        assert stats["n"] == "3"


class TestScatter:
    def panel(self):
        return DashboardPanel(
            kind="scatter",
            title="Landings",
            x="endpoint_x_dva",
            y="endpoint_y_dva",
            target_x="target_x_dva",
            target_y="target_y_dva",
        )

    def test_points_targets_and_error_are_reported(self):
        rows = [
            trial(1, endpoint_x_dva=5.5, endpoint_y_dva=0.0, target_x_dva=5.0, target_y_dva=0.0),
            trial(2, endpoint_x_dva=4.5, endpoint_y_dva=0.0, target_x_dva=5.0, target_y_dva=0.0),
            trial(3, endpoint_x_dva=-5.0, endpoint_y_dva=1.0, target_x_dva=-5.0, target_y_dva=0.0),
        ]
        data = payload(self.panel(), rows)

        assert data["form"] == "scatter"
        # One unnamed series when nothing colours the panel.
        assert [s["name"] for s in data["series"]] == [""]
        assert len(data["series"][0]["points"]) == 3
        # Two distinct targets, each listed once however many trials used it.
        assert sorted(data["targets"]) == [[-5.0, 0.0], [5.0, 0.0]]
        assert data["equal_aspect"] is True
        stats = {stat["label"]: stat["value"] for stat in data["stats"]}
        assert stats["median error"] == "0.5 dva"

    def test_a_mean_landing_is_reported_only_where_it_is_a_position(self):
        # With one target the mean is where the responses cluster. With two it
        # falls between the clusters — on the fixation point, where nothing
        # landed — so it is not reported at all.
        one = [
            trial(1, endpoint_x_dva=5.5, endpoint_y_dva=0.0, target_x_dva=5.0, target_y_dva=0.0),
            trial(2, endpoint_x_dva=4.5, endpoint_y_dva=1.0, target_x_dva=5.0, target_y_dva=0.0),
        ]
        two = one + [
            trial(3, endpoint_x_dva=-5.0, endpoint_y_dva=0.0, target_x_dva=-5.0, target_y_dva=0.0)
        ]

        centroid = payload(self.panel(), one)["series"][0]["centroid"]
        assert centroid == [pytest.approx(5.0), pytest.approx(0.5)]
        assert "centroid" not in payload(self.panel(), two)["series"][0]

    def test_rows_missing_either_coordinate_are_dropped(self):
        rows = [
            trial(1, endpoint_x_dva=1.0, endpoint_y_dva=2.0),
            trial(2, endpoint_x_dva=1.0),
            trial(3),
        ]
        assert len(payload(self.panel(), rows)["series"][0]["points"]) == 1

    def test_a_boolean_column_is_not_plotted_as_one_and_zero(self):
        # bool is an int subclass; without an explicit guard every True/False
        # column silently becomes a numeric series.
        rows = [trial(1, endpoint_x_dva=True, endpoint_y_dva=False)]
        assert payload(self.panel(), rows)["form"] == "empty"


class TestVectors:
    """Landing relative to fixation: every trial collapsed onto one origin, so
    amplitude and direction are readable even when the fixation point moves."""

    def panel(self, **kwargs):
        fields = {
            "kind": "vectors",
            "title": "Relative to fixation",
            "x": "endpoint_x_dva",
            "y": "endpoint_y_dva",
        }
        return DashboardPanel(**{**fields, **kwargs})

    def test_each_point_is_measured_from_its_own_trials_origin(self):
        rows = [
            trial(1, endpoint_x_dva=9.0, endpoint_y_dva=1.0, fix_x_dva=1.0, fix_y_dva=1.0),
            trial(2, endpoint_x_dva=-3.0, endpoint_y_dva=0.0, fix_x_dva=5.0, fix_y_dva=0.0),
        ]
        panel = self.panel(origin_x="fix_x_dva", origin_y="fix_y_dva")
        data = payload(panel, rows)

        assert data["form"] == "vectors"
        assert data["series"][0]["points"] == [[8.0, 0.0], [-8.0, 0.0]]
        assert "note" not in data

    def test_a_task_with_a_fixed_fixation_point_says_what_it_assumed(self):
        # Screen centre is where this framework's fixation point sits, so a
        # task that never moves it writes no column for it. Assuming (0, 0) is
        # right; assuming it silently is not.
        rows = [trial(1, endpoint_x_dva=8.0, endpoint_y_dva=0.0)]
        data = payload(self.panel(origin_x="fixation_x_dva", origin_y="fixation_y_dva"), rows)

        assert data["series"][0]["points"] == [[8.0, 0.0]]
        assert data["note"] == "origin assumed at screen centre (fixation_x_dva is not recorded)"

    def test_a_trial_whose_own_origin_is_missing_is_dropped(self):
        # Plotting it against somebody else's origin would be a guess.
        rows = [
            trial(1, endpoint_x_dva=9.0, endpoint_y_dva=0.0, fix_x_dva=1.0, fix_y_dva=0.0),
            trial(2, endpoint_x_dva=9.0, endpoint_y_dva=0.0),
        ]
        panel = self.panel(origin_x="fix_x_dva", origin_y="fix_y_dva")
        assert payload(panel, rows)["series"][0]["points"] == [[8.0, 0.0]]

    def test_amplitude_rings_land_on_readable_numbers(self):
        rows = [
            trial(i, endpoint_x_dva=10.0 * (1 if i % 2 else -1), endpoint_y_dva=0.0)
            for i in range(1, 21)
        ]
        data = payload(self.panel(), rows)

        assert data["rings"] == [
            pytest.approx(2.5),
            pytest.approx(5.0),
            pytest.approx(7.5),
            pytest.approx(10.0),
        ]
        assert data["radius"] > 10.0
        stats = {stat["label"]: stat["value"] for stat in data["stats"]}
        assert stats["median amplitude"] == "10.0 dva"
        assert stats["n"] == "20"

    def test_the_axes_carry_the_units_of_the_columns(self):
        rows = [trial(1, endpoint_x_dva=8.0, endpoint_y_dva=0.0)]
        data = payload(self.panel(), rows)
        assert data["x_label"] == "horizontal displacement (dva)"
        assert data["y_label"] == "vertical displacement (dva)"


class TestSeries:
    def test_a_moving_mean_rides_on_top_of_the_raw_trace(self):
        rows = [trial(i, gain=float(i % 4)) for i in range(1, 41)]
        data = payload(DashboardPanel(kind="series", title="Gain", value="gain"), rows)

        assert [s["name"] for s in data["series"]][0] == "gain"
        assert data["series"][0]["marker"] is True and data["series"][0]["line"] is False
        assert data["series"][1]["name"].startswith("moving mean")
        assert data["series"][1]["line"] is True

    def test_short_sessions_get_no_smoothing_line(self):
        rows = [trial(i, gain=1.0) for i in range(1, 6)]
        data = payload(DashboardPanel(kind="series", title="Gain", value="gain"), rows)
        assert len(data["series"]) == 1

    def test_long_sessions_are_thinned_to_a_drawable_size(self):
        rows = [trial(i, gain=float(i)) for i in range(1, 2001)]
        data = payload(DashboardPanel(kind="series", title="Gain", value="gain"), rows)

        for series in data["series"]:
            assert len(series["points"]) <= MAX_POINTS
            # Thinning never loses the ends: the newest value is what a live
            # dashboard is being watched for.
            assert series["points"][-1][0] == 2000
            assert series["points"][0][0] == 1

    def test_x_falls_back_to_position_when_a_record_has_no_trial_index(self):
        rows = [{"gain": 1.0}, {"gain": 2.0}]
        data = payload(DashboardPanel(kind="series", title="Gain", value="gain"), rows)
        assert [p[0] for p in data["series"][0]["points"]] == [1.0, 2.0]


class TestGroupedMean:
    def panel(self):
        return DashboardPanel(
            kind="grouped_mean", title="Bias", value="bias_dva", group="coherence"
        )

    def test_mean_sem_and_n_per_group(self):
        rows = [
            trial(1, coherence=0.5, bias_dva=1.0),
            trial(2, coherence=0.5, bias_dva=3.0),
            trial(3, coherence=0.1, bias_dva=2.0),
        ]
        data = payload(self.panel(), rows)

        assert data["form"] == "dots"
        by_label = {group["label"]: group for group in data["groups"]}
        assert by_label["0.5"]["mean"] == pytest.approx(2.0)
        # SD of {1, 3} is sqrt(2); SEM is that over sqrt(2).
        assert by_label["0.5"]["sem"] == pytest.approx(1.0)
        assert by_label["0.5"]["n"] == 2

    def test_a_single_observation_reports_no_error_bar_rather_than_a_zero_one(self):
        rows = [trial(1, coherence=0.2, bias_dva=4.0)]
        assert payload(self.panel(), rows)["groups"][0]["sem"] is None

    def test_numeric_levels_sort_as_numbers(self):
        # The string order "0.2" < "0.4" < "10" happens to be wrong exactly
        # when a level reaches double digits.
        rows = [trial(i, coherence=c, bias_dva=1.0) for i, c in enumerate([10, 2, 0.4])]
        labels = [group["label"] for group in payload(self.panel(), rows)["groups"]]
        assert labels == ["0.4", "2", "10"]


class TestPerformance:
    def panel(self):
        return DashboardPanel(kind="performance", title="Performance")

    def test_running_accuracy_uses_scored_trials_only(self):
        rows = [
            trial(1, completed=True, success=True),
            trial(2, completed=False),  # broken fixation: not scored, not counted
            trial(3, completed=True, success=False),
            trial(4, completed=True, success=True),
        ]
        data = payload(self.panel(), rows)
        cumulative = data["series"][0]["points"]

        assert data["y_label"] == "proportion correct"
        assert [p[0] for p in cumulative] == [1, 3, 4]
        assert [p[1] for p in cumulative] == [1.0, 0.5, pytest.approx(2 / 3)]
        assert data["y_domain"] == [0.0, 1.0]

    def test_a_task_without_success_falls_back_to_completion_and_says_so(self):
        rows = [trial(1, completed=True), trial(2, completed=False), trial(3, completed=True)]
        data = payload(self.panel(), rows)

        assert data["y_label"] == "proportion completed"
        assert [round(p[1], 3) for p in data["series"][0]["points"]] == [1.0, 0.5, 0.667]

    def test_the_confidence_band_brackets_the_estimate_and_stays_in_range(self):
        rows = [trial(i, completed=True, success=i % 3 != 0) for i in range(1, 41)]
        data = payload(self.panel(), rows)
        estimate = {p[0]: p[1] for p in data["series"][0]["points"]}

        for x, low, high in data["band"]["points"]:
            assert 0.0 <= low <= estimate[x] <= high <= 1.0
        # A single trial cannot produce a zero-width interval.
        assert data["band"]["points"][0][1] < data["band"]["points"][0][2]

    def test_the_band_is_thinned_with_the_series_it_belongs_to(self):
        rows = [trial(i, completed=True, success=True) for i in range(1, 1001)]
        data = payload(self.panel(), rows)
        assert [p[0] for p in data["band"]["points"]] == [p[0] for p in data["series"][0]["points"]]

    def test_no_trials_is_a_message_not_an_empty_axis(self):
        assert payload(self.panel())["form"] == "empty"


class TestReward:
    def panel(self):
        return DashboardPanel(kind="rewards", title="Reward earned")

    def test_cumulative_valve_open_time_steps_at_each_delivery(self):
        events = [reward_event(2), reward_event(5, n_pulses=1)]
        data = payload(self.panel(), events=events)
        points = data["series"][0]["points"]

        assert data["y_label"] == "cumulative valve-open time (s)"
        assert data["series"][0]["step"] is True
        # 2x200 ms = 0.4 s, then 1x200 ms more.
        assert points == [[1.0, 0.0], [2.0, 0.4], [5.0, pytest.approx(0.6)]]

    def test_the_curve_is_carried_to_the_present_trial(self):
        # Without this the line stops at the last delivery, and a subject that
        # stopped working ten minutes ago looks like one still earning.
        events = [reward_event(2), {"trial_index": 40, "event": "TRIAL_END", "t": 40.0}]
        points = payload(self.panel(), events=events)["series"][0]["points"]
        assert points[-1] == [40.0, pytest.approx(0.4)]

    def test_totals_are_numbers_not_bars(self):
        events = [reward_event(1), reward_event(2, manual=True)]
        events.append({"trial_index": 3, "event": "NO_REWARD", "t": 3.0, "payload_json": "{}"})
        events.append({"trial_index": 4, "event": "REWARD_FAILED", "t": 4.0, "payload_json": "{}"})
        data = payload(self.panel(), events=events)
        stats = {stat["label"]: stat["value"] for stat in data["stats"]}

        assert stats["total"] == "0.8 s"
        assert stats["deliveries"] == "2"
        assert stats["manual"] == "1"
        assert stats["unrewarded"] == "1"
        assert stats["failed"] == "1"

    def test_a_failed_delivery_is_marked_on_the_curve(self):
        events = [reward_event(1), {"trial_index": 3, "event": "REWARD_FAILED", "t": 3.0}]
        marks = payload(self.panel(), events=events)["marks"]
        assert marks == [{"x": 3.0, "y": pytest.approx(0.4), "kind": "failure"}]

    def test_a_delivery_with_no_pulse_train_is_counted_never_priced(self):
        events = [
            reward_event(1),
            {"trial_index": 2, "event": "REWARD", "t": 2.0, "payload_json": "{}"},
        ]
        data = payload(self.panel(), events=events)

        assert data["y_label"] == "cumulative deliveries"
        assert data["series"][0]["points"][-1][1] == 2
        assert "no pulse train" in data["note"]

    def test_no_reward_yet_is_a_message(self):
        events = [{"trial_index": 1, "event": "TRIAL_END", "t": 0.0}]
        assert payload(self.panel(), events=events) == {
            "form": "empty",
            "message": "No reward delivered yet",
        }


class TestStat:
    def test_a_scalar_is_shown_as_a_scalar(self):
        rows = [trial(i, rt_ms=v) for i, v in enumerate([100, 200, 300])]
        data = payload(
            DashboardPanel(kind="stat", title="Median RT", value="rt_ms", agg="median"), rows
        )

        assert data["form"] == "stat"
        assert data["value"] == "200"
        assert data["unit"] == "ms"
        assert data["secondary"].startswith("n = 3")

    @pytest.mark.parametrize(
        ("agg", "expected"), [("mean", "200"), ("sum", "600"), ("min", "100"), ("last", "300")]
    )
    def test_each_aggregate(self, agg, expected):
        rows = [trial(i, rt_ms=v) for i, v in enumerate([100, 200, 300])]
        panel = DashboardPanel(kind="stat", title="RT", value="rt_ms", agg=agg)
        assert payload(panel, rows)["value"] == expected

    def test_count_works_on_a_column_that_is_not_a_number(self):
        rows = [trial(1, response_key="left"), trial(2), trial(3, response_key="right")]
        panel = DashboardPanel(kind="stat", title="Responses", value="response_key", agg="count")
        data = payload(panel, rows)
        assert data["value"] == "2"
        assert data["secondary"] == "of 3 trials"


class TestConditionColours:
    """A condition the experiment varies is what the experimenter is watching
    for, so the spatial panels are split by it without anyone declaring it."""

    def panel(self, color_by):
        return DashboardPanel(
            kind="scatter",
            title="Landings",
            x="endpoint_x_dva",
            y="endpoint_y_dva",
            color_by=color_by,
        )

    def rows(self, levels):
        return [
            trial(i, side=level, endpoint_x_dva=float(i), endpoint_y_dva=0.0)
            for i, level in enumerate(levels, start=1)
        ]

    def test_named_levels_take_separate_hues(self):
        data = payload(self.panel("side"), self.rows(["left", "right", "left"]))
        series = {s["name"]: s for s in data["series"]}

        assert sorted(series) == ["left", "right"]
        # Unordered levels: separate slots, no ramp position to read.
        assert {s["slot"] for s in data["series"]} == {1, 2}
        assert all("ramp" not in s for s in data["series"])
        assert len(series["left"]["points"]) == 2

    def test_numeric_levels_take_one_hue_in_order(self):
        # 0.05 really is less than 0.4, and the reader should see that in the
        # colour rather than have to look it up in a legend.
        rows = [
            trial(i, side=level, endpoint_x_dva=1.0, endpoint_y_dva=0.0)
            for i, level in enumerate([0.4, 0.05, 0.2], start=1)
        ]
        data = payload(self.panel("side"), rows)

        assert [s["name"] for s in data["series"]] == ["0.05", "0.2", "0.4"]
        assert [s["ramp"] for s in data["series"]] == [0, 2, 4]
        assert all("slot" not in s for s in data["series"])

    def test_levels_past_the_validated_set_fold_and_say_so(self):
        # A colour beyond the validated set is one the reader cannot reliably
        # tell from another; inventing one is how a legend starts lying.
        data = payload(self.panel("side"), self.rows(["a", "b", "c", "d", "e"]))

        assert [s["name"] for s in data["series"][:3]] == ["a", "b", "c"]
        assert data["series"][-1]["muted"] is True
        assert data["series"][-1]["name"] == "other (2 levels)"
        assert data["note"] == "2 further side levels folded into one colour"

    def test_one_unlabelled_trial_does_not_flip_an_ordered_factor(self):
        # It used to: "missing" is not a number, so a single unlabelled trial
        # turned a light-to-dark ramp into a set of unordered hues.
        rows = [
            trial(1, side=0.1, endpoint_x_dva=1.0, endpoint_y_dva=0.0),
            trial(2, side=0.4, endpoint_x_dva=1.0, endpoint_y_dva=0.0),
            trial(3, endpoint_x_dva=1.0, endpoint_y_dva=0.0),
        ]
        data = payload(self.panel("side"), rows)
        levels = [s for s in data["series"] if not s.get("muted")]

        assert [s["name"] for s in levels] == ["0.1", "0.4"]
        assert all("ramp" in s for s in levels)
        # The unlabelled trial is shown, because it happened — just not as a
        # level of a factor it was never given.
        unlabelled = data["series"][-1]
        assert unlabelled["name"] == "side missing"
        assert unlabelled["muted"] is True
        assert unlabelled["points"] == [[1.0, 0.0]]

    def test_each_level_gets_its_own_mean(self):
        rows = [
            trial(1, side="left", endpoint_x_dva=-8.0, endpoint_y_dva=0.0),
            trial(2, side="left", endpoint_x_dva=-6.0, endpoint_y_dva=0.0),
            trial(3, side="right", endpoint_x_dva=8.0, endpoint_y_dva=0.0),
        ]
        data = payload(self.panel("side"), rows)
        means = {s["name"]: s["centroid"][0] for s in data["series"]}

        # Averaging the two clusters together would put the mean at 2/3 — on
        # the fixation point, where no saccade ever landed.
        assert means == {"left": pytest.approx(-7.0), "right": pytest.approx(8.0)}

    def test_the_column_travels_so_the_readout_can_name_it(self):
        data = payload(self.panel("side"), self.rows(["left"]))
        assert data["color_label"] == "side"

    def test_no_condition_means_one_unnamed_series(self):
        data = payload(self.panel(None), self.rows(["left", "right"]))
        assert [s["name"] for s in data["series"]] == [""]


class TestGroupedRate:
    """Accuracy by condition: the panel an experimenter reads to see whether
    one level of their factor is harder than another."""

    def panel(self, **kwargs):
        return DashboardPanel(kind="grouped_rate", title="Accuracy by side", group="side", **kwargs)

    def test_proportion_correct_per_level_with_an_asymmetric_interval(self):
        rows = [trial(i, side="left", completed=True, success=i < 8) for i in range(1, 11)]
        rows += [trial(i, side="right", completed=True, success=True) for i in range(11, 15)]
        data = payload(self.panel(), rows)
        groups = {group["label"]: group for group in data["groups"]}

        assert data["form"] == "dots" and data["style"] == "bars"
        assert data["y_domain"] == [0.0, 1.0]
        assert groups["left"]["mean"] == pytest.approx(0.7)
        assert groups["left"]["n"] == 10
        # Wilson, so four-for-four does not report certainty.
        assert groups["right"]["mean"] == 1.0
        assert groups["right"]["low"] < 1.0
        assert groups["right"]["high"] == pytest.approx(1.0)
        # ...and it is not symmetric about the estimate.
        upper = groups["left"]["high"] - groups["left"]["mean"]
        lower = groups["left"]["mean"] - groups["left"]["low"]
        assert upper != pytest.approx(lower)

    def test_a_task_without_success_falls_back_to_completion_and_says_so(self):
        rows = [trial(i, side="left", completed=i % 2 == 0) for i in range(1, 11)]
        data = payload(self.panel(), rows)

        assert data["y_label"] == "proportion completed"
        assert data["groups"][0]["mean"] == pytest.approx(0.5)

    def test_trials_with_no_level_are_not_a_level(self):
        rows = [
            trial(1, side="left", completed=True, success=True),
            trial(2, completed=True, success=False),
        ]
        data = payload(self.panel(), rows)
        assert [group["label"] for group in data["groups"]] == ["left"]

    def test_nothing_scored_yet_is_a_message(self):
        assert payload(self.panel(), [])["form"] == "empty"


class TestGroupedStyle:
    def test_a_mean_panel_can_be_drawn_as_bars(self):
        rows = [trial(i, side="left", bias_dva=2.0) for i in range(1, 5)]
        panel = DashboardPanel(
            kind="grouped_mean", title="Bias", value="bias_dva", group="side", style="bars"
        )
        assert payload(panel, rows)["style"] == "bars"

    def test_dots_are_the_default_for_a_signed_mean(self):
        rows = [trial(i, side="left", bias_dva=2.0) for i in range(1, 5)]
        panel = DashboardPanel(kind="grouped_mean", title="Bias", value="bias_dva", group="side")
        assert payload(panel, rows)["style"] == "dots"


class TestValidation:
    @pytest.mark.parametrize(
        ("kind", "message"),
        [
            ("histogram", "require value"),
            ("stat", "require value"),
            ("scatter", "require x and y"),
            ("grouped_mean", "require value"),
        ],
    )
    def test_a_panel_cannot_be_built_without_the_fields_its_kind_needs(self, kind, message):
        with pytest.raises(ValueError, match=message):
            DashboardPanel(kind=kind, title="Broken")

    def test_a_panel_forced_past_its_validator_fails_loudly(self):
        # model_construct skips validation, which is the only way to reach the
        # guard — an empty plot with no explanation would be worse.
        panel = DashboardPanel.model_construct(kind="histogram", title="Broken", value=None)
        with pytest.raises(ValueError, match="has no value"):
            panel_payload(panel, [trial(1, rt_ms=1.0)], [])


class TestDefaults:
    def test_every_default_panel_renders_from_a_realistic_session(self):
        trials = [
            trial(
                i,
                outcome="CORRECT" if i % 4 else "FIX_BREAK",
                completed=i % 4 != 0,
                success=i % 4 != 0,
                rt_ms=280 + (i % 17) * 9,
                response_key="left" if i % 2 else "right",
                endpoint_x_dva=5.0 + (i % 5) * 0.2,
                endpoint_y_dva=(i % 3) * 0.3,
                target_x_dva=5.0,
                target_y_dva=0.0,
                fixation_x_dva=0.0,
                fixation_y_dva=0.0,
            )
            for i in range(1, 61)
        ]
        events = [reward_event(i) for i in range(1, 61) if i % 4]

        forms = {
            panel.kind: panel_payload(panel, trials, events)["form"] for panel in DEFAULT_PANELS
        }
        assert forms == {
            "performance": "line",
            "rewards": "line",
            "outcomes": "bars",
            "histogram": "histogram",
            "responses": "bars",
            "scatter": "scatter",
            "vectors": "vectors",
        }

    def test_every_default_panel_survives_an_empty_session(self):
        for panel in DEFAULT_PANELS:
            assert panel_payload(panel, [], [])["form"] == "empty"


# ----------------------------------------------------------------------
# Several factors on one axis, and panels that show part of the session
# ----------------------------------------------------------------------


def _cells():
    """Four trials covering two factors, with a value that differs by both."""
    return [
        trial(0, completed=True, hit=1.0, alignment="aligned", separation="near"),
        trial(1, completed=True, hit=1.0, alignment="aligned", separation="far"),
        trial(2, completed=True, hit=0.0, alignment="rotated", separation="near"),
        trial(3, completed=True, hit=0.0, alignment="rotated", separation="far"),
    ]


def test_grouped_mean_over_several_factors_puts_them_on_one_axis():
    """Four proportions on four axes cannot be compared by eye; the same four
    on one axis can. Each bar keeps the name of the factor it came from, which
    is what the colours and the legend are drawn from."""
    panel = DashboardPanel(
        kind="grouped_mean",
        title="P(occluder)",
        value="hit",
        group=("alignment", "separation"),
        style="bars",
    )
    data = panel_payload(panel, _cells(), [])

    assert [(g["series"], g["label"], g["mean"]) for g in data["groups"]] == [
        ("alignment", "aligned", 1.0),
        ("alignment", "rotated", 0.0),
        ("separation", "far", 0.5),
        ("separation", "near", 0.5),
    ]
    # Declared order is kept, so a factor's own bars stay side by side.
    assert [g["series"] for g in data["groups"]] == ["alignment"] * 2 + ["separation"] * 2
    # The x axis is no longer one factor's name, so it does not claim to be.
    assert data["x_label"] == "condition"


def test_one_factor_still_labels_its_own_axis():
    """The common case is unchanged — a single factor keeps its axis label and
    needs no legend to say what the bars are."""
    panel = DashboardPanel(
        kind="grouped_mean", title="P", value="hit", group="alignment", style="bars"
    )
    data = panel_payload(panel, _cells(), [])
    assert data["x_label"] == "alignment"
    assert [g["label"] for g in data["groups"]] == ["aligned", "rotated"]


def test_two_factors_sharing_a_level_name_are_not_merged():
    """`near` under one factor and `near` under another are different bars.
    Bucketing by level alone would average unrelated trials into one number
    and nothing about the panel would look wrong."""
    rows = [
        trial(0, completed=True, hit=1.0, size="near", distance="near"),
        trial(1, completed=True, hit=0.0, size="far", distance="near"),
    ]
    panel = DashboardPanel(kind="grouped_mean", title="P", value="hit", group=("size", "distance"))
    data = panel_payload(panel, rows, [])
    assert [(g["series"], g["label"], g["mean"], g["n"]) for g in data["groups"]] == [
        ("size", "far", 0.0, 1),
        ("size", "near", 1.0, 1),
        ("distance", "near", 0.5, 2),
    ]


def test_grouped_rate_refuses_several_factors():
    """Its bars are proportions of the same trials split one way. Several
    factors at once would put every trial in several bars."""
    with pytest.raises(ValueError, match="grouped_rate takes one group"):
        DashboardPanel(kind="grouped_rate", title="Accuracy", group=("a", "b"))


def test_where_shows_only_the_trials_it_names():
    """A landing plot per separation, rather than one pooled panel that hides
    the difference being looked for."""
    panel = DashboardPanel(
        kind="grouped_mean",
        title="P, far only",
        value="hit",
        group="alignment",
        where={"separation": "far"},
    )
    rows = select_rows(panel, _cells())
    assert [row["trial_index"] for row in rows] == [1, 3]


def test_where_compares_as_text_so_numeric_levels_match():
    """A level written as 8.25 in the record is named "8.25" in a config."""
    rows = [trial(0, separation_dva=3.75), trial(1, separation_dva=8.25)]
    panel = DashboardPanel(kind="histogram", title="h", value="x", where={"separation_dva": "8.25"})
    assert [row["trial_index"] for row in select_rows(panel, rows)] == [1]


def test_where_on_a_column_the_task_never_writes_matches_nothing():
    """Empty and captioned, rather than quietly showing every trial — a filter
    that silently does nothing is worse than one that fails."""
    panel = DashboardPanel(
        kind="grouped_mean",
        title="P",
        value="hit",
        group="alignment",
        where={"nonexistent": "far"},
    )
    assert select_rows(panel, _cells()) == []
    assert panel_payload(panel, _cells(), [])["form"] == "empty"


def test_where_combines_with_completed_only():
    panel = DashboardPanel(
        kind="grouped_mean",
        title="P",
        value="hit",
        group="alignment",
        completed_only=True,
        where={"separation": "near"},
    )
    rows = _cells() + [trial(4, completed=False, hit=1.0, alignment="aligned", separation="near")]
    assert [row["trial_index"] for row in select_rows(panel, rows)] == [0, 2]
