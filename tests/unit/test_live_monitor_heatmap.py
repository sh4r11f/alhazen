"""The heatmap wire form, checked before it leaves Python.

A heatmap reaches the page as a finished payload an experiment built by hand.
The page can only answer a malformed one with a card saying "Malformed map";
``check_heatmap`` refuses it when the session publishes it, naming the field.
"""

from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from alhazen import LiveMonitorSpec
from alhazen.errors import SessionError
from alhazen.live_monitor.heatmap import check_heatmap
from alhazen.live_monitor.presentation import present
from alhazen.live_monitor.runtime import live_monitor_state


def rf_map() -> dict:
    """A receptive-field plate as rf-mapping sends it (rf_mapping/livemap.py):
    degrees on both axes, none of the newer fields."""
    return {
        "form": "heatmap",
        "maps": [
            {"name": "population", "matrix": [[1.5, 2.0, None], [0.0, 4.5, 3.0]]},
            {"name": "ch 3", "matrix": [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0]], "centroid": [0.1, 0.2]},
        ],
        "x_edges": [-1.5, -0.5, 0.5, 1.5],
        "y_edges": [-1.0, 0.0, 1.0],
        "flashes": [[3, 4, 0], [1, 2, 5]],
        "vmax": 5.0,
        "x_label": "azimuth (dva)",
        "y_label": "elevation (dva)",
        "value_label": "spikes/s",
    }


def search_slice() -> dict:
    """A posterior slice in the new form, as mbri would send it: real edges on
    two log axes, a unit per axis, and a colour range."""
    speed = np.geomspace(4.0, 32.0, 4).tolist()
    density = np.geomspace(0.2, 3.0, 3).tolist()
    return {
        "form": "heatmap",
        "maps": [{"name": "posterior mean", "matrix": [[0.31, 0.35, 0.40], [0.33, 0.45, 0.49]]}],
        "x_edges": speed,
        "y_edges": density,
        "x_scale": "log",
        "y_scale": "log",
        "x_unit": "dva/s",
        "y_unit": "dots/dva²",
        "x_label": "speed (dva/s)",
        "y_label": "dot density (dots/dva²)",
        "value_label": "balanced accuracy",
        "vmin": 0.30,
        "vmax": 0.49,
    }


def refused(payload: dict, match: str) -> None:
    with pytest.raises(SessionError, match=match):
        check_heatmap(payload)


class TestWellFormed:
    def test_a_receptive_field_plate_in_the_old_form_passes(self):
        check_heatmap(rf_map())

    def test_a_slice_with_log_axes_units_and_a_colour_range_passes(self):
        check_heatmap(search_slice())

    def test_a_map_with_no_cells_yet_passes_with_no_edges(self):
        # The live analysis's first publish: the page draws "No map yet".
        check_heatmap({"form": "heatmap", "maps": [], "x_edges": [], "y_edges": []})
        check_heatmap({"form": "heatmap", "maps": [{"name": "population", "matrix": []}]})

    def test_numpy_numbers_count_as_numbers(self):
        payload = search_slice()
        payload["vmin"] = np.float64(0.3)
        payload["maps"][0]["matrix"][0][0] = np.float64(0.31)
        check_heatmap(payload)

    def test_the_check_leaves_the_payload_as_it_was(self):
        payload = search_slice()
        before = copy.deepcopy(payload)
        check_heatmap(payload)
        assert payload == before


class TestScales:
    @pytest.mark.parametrize("scale", ["Log", "logarithmic", "ln", "", 10])
    def test_an_unknown_scale_is_refused_naming_the_two_there_are(self, scale):
        payload = search_slice()
        payload["y_scale"] = scale
        refused(payload, r"y_scale is .*; it must be one of 'linear', 'log'")

    @pytest.mark.parametrize(("given", "missing"), [("x", "y"), ("y", "x")])
    def test_one_scale_without_the_other_is_refused(self, given, missing):
        payload = rf_map()
        payload[f"{given}_scale"] = "linear"
        refused(payload, f"gives {given}_scale but not {missing}_scale")

    @pytest.mark.parametrize("first_edge", [0.0, -0.2])
    def test_a_log_axis_with_an_edge_at_or_below_zero_is_refused(self, first_edge):
        payload = search_slice()
        payload["y_edges"][0] = first_edge
        refused(payload, r"y_edges starts at .*, but y_scale is 'log': every edge .* above 0")

    def test_a_linear_axis_may_cross_zero(self):
        payload = rf_map()
        payload.update(x_scale="linear", y_scale="linear")
        check_heatmap(payload)


class TestEdges:
    def test_edges_one_short_are_refused_naming_the_count_needed(self):
        payload = rf_map()
        payload["x_edges"] = [-1.5, -0.5, 0.5]
        refused(payload, r"x_edges has 3 values, but the matrix is 3 cell\(s\) wide: it needs 4")

    def test_edges_one_too_many_are_refused(self):
        payload = rf_map()
        payload["y_edges"] = [-1.0, 0.0, 1.0, 2.0]
        refused(payload, r"y_edges has 4 values, but the matrix is 2 cell\(s\) tall: it needs 3")

    def test_missing_edges_are_refused_when_there_are_cells(self):
        payload = rf_map()
        del payload["y_edges"]
        refused(payload, r"y_edges is missing; the matrix is 2 cell\(s\) tall, so it needs 3")

    @pytest.mark.parametrize("edges", [[0.0, 2.0, 1.0, 3.0], [0.0, 1.0, 1.0, 3.0]])
    def test_edges_that_do_not_increase_are_refused(self, edges):
        payload = rf_map()
        payload["x_edges"] = edges
        refused(payload, "x_edges must be strictly increasing")

    @pytest.mark.parametrize("edge", [float("nan"), float("inf"), "1", None, True])
    def test_an_edge_that_is_not_a_finite_number_is_refused(self, edge):
        payload = rf_map()
        payload["x_edges"][1] = edge
        refused(payload, r"x_edges\[1\] is .*; an edge is a finite number")

    def test_edges_as_text_are_refused(self):
        payload = rf_map()
        payload["x_edges"] = "0123"
        refused(payload, "x_edges must be a list of numbers")


class TestMaps:
    def test_maps_of_different_shapes_are_refused(self):
        payload = rf_map()
        payload["maps"][1]["matrix"] = [[0.0, 1.0], [2.0, 3.0]]
        refused(payload, r"maps\[1\] is 2x2 cells but maps\[0\] is 2x3")

    def test_ragged_rows_are_refused(self):
        payload = rf_map()
        payload["maps"][0]["matrix"] = [[1.0, 2.0, 3.0], [4.0, 5.0]]
        refused(payload, r"maps\[0\] has rows of different lengths \[2, 3\]")

    @pytest.mark.parametrize("cell", ["4.5", True, [1.0], {"v": 1}])
    def test_a_cell_that_is_not_a_number_or_none_is_refused(self, cell):
        payload = rf_map()
        payload["maps"][1]["matrix"][1][2] = cell
        refused(payload, r"maps\[1\] cell \[1\]\[2\] is .*; a cell is a finite number, or None")

    @pytest.mark.parametrize("cell", [float("nan"), float("inf"), np.float64("-inf")])
    def test_a_cell_that_is_not_finite_is_refused(self, cell):
        # JSON has no NaN: one in a published state breaks the live page's
        # reading of the whole update. A cell not measured yet is None.
        payload = rf_map()
        payload["maps"][0]["matrix"][0][0] = cell
        refused(payload, r"maps\[0\] cell \[0\]\[0\] is .*; a cell is a finite number, or None")

    def test_a_map_without_a_matrix_is_refused(self):
        payload = rf_map()
        payload["maps"].append({"name": "ch 9"})
        refused(payload, r"maps\[2\] must be a dict with a 'matrix' list of rows")

    def test_maps_that_are_not_a_list_are_refused(self):
        payload = rf_map()
        payload["maps"] = {"name": "population"}
        refused(payload, "maps must be a list of maps, got dict")


class TestUnits:
    @pytest.mark.parametrize("unit", [1, ["dva"], b"dva"])
    def test_a_unit_that_is_not_text_is_refused(self, unit):
        payload = search_slice()
        payload["x_unit"] = unit
        refused(payload, "x_unit must be text")

    def test_the_presentation_pass_leaves_units_and_scales_as_given(self):
        # A unit is a symbol, written as the experiment wrote it; only the
        # prose around it (axis titles, notes) is rewritten for print.
        presented = present(search_slice())
        assert presented["x_unit"] == "dva/s"
        assert presented["y_unit"] == "dots/dva²"
        assert (presented["x_scale"], presented["y_scale"]) == ("log", "log")
        assert presented["x_label"] == "Speed (dva/s)"


class TestColourRange:
    def test_a_range_the_wrong_way_round_is_refused(self):
        payload = search_slice()
        payload.update(vmin=0.49, vmax=0.30)
        refused(payload, "colour range is the wrong way round: vmin 0.49 is not below vmax 0.3")

    def test_an_empty_range_is_refused_and_called_empty(self):
        # What a flat surface gives when its limits are its own min and max.
        payload = search_slice()
        payload.update(vmin=0.4, vmax=0.4)
        refused(payload, "colour range is empty: vmin and vmax are both 0.4")

    def test_vmin_without_vmax_is_refused(self):
        payload = search_slice()
        del payload["vmax"]
        refused(payload, "gives vmin but no vmax")

    @pytest.mark.parametrize("key", ["vmin", "vmax"])
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), "0.3", True])
    def test_a_limit_that_is_not_a_finite_number_is_refused(self, key, value):
        payload = search_slice()
        payload[key] = value
        refused(payload, f"{key} must be a finite number")

    def test_vmax_alone_keeps_the_old_scale_from_zero_and_may_be_zero(self):
        # rf-mapping sends vmax = 0 until a cell has a spike.
        payload = rf_map()
        payload["vmax"] = 0.0
        check_heatmap(payload)


class TestPublishing:
    """live_monitor_state() checks every heatmap among a session's extra panels."""

    def publish(self, *extra):
        return live_monitor_state(
            revision=1,
            status="running",
            identity={"task_name": "t", "subject": "s1", "session": 1, "run": 1},
            trials=[],
            events=[],
            spec=LiveMonitorSpec(include_defaults=False),
            extra_panels=list(extra),
        )

    def test_called_directly_a_malformed_heatmap_raises_naming_its_panel(self):
        # The default: a test or a tool building a state gets the exception.
        payload = search_slice()
        payload["vmin"], payload["vmax"] = 0.5, 0.3
        with pytest.raises(SessionError, match=r"panel 'Posterior slice': heatmap colour range"):
            self.publish({"title": "Posterior slice", "data": payload})

    def test_with_a_reporter_a_malformed_heatmap_becomes_an_error_card(self):
        """How a session publishes: the problem is reported and drawn, and
        nothing is raised."""
        reported: list[tuple[str, str]] = []
        payload = search_slice()
        payload["x_edges"] = payload["x_edges"][:-1]
        state = live_monitor_state(
            revision=1,
            status="running",
            identity={},
            trials=[],
            events=[],
            spec=LiveMonitorSpec(include_defaults=False),
            extra_panels=[
                {"title": "Posterior slice", "section": "Search", "data": payload},
                {"title": "Receptive fields", "data": rf_map()},
            ],
            on_invalid_panel=lambda title, problem: reported.append((title, problem)),
        )
        problem = (
            "heatmap x_edges has 3 values, but the matrix is 3 cell(s) wide: it needs 4, "
            "one more than the cells"
        )
        assert reported == [("Posterior slice", problem)]
        card, untouched = state["panels"]
        # The panel keeps its title and section; its data says what is wrong,
        # naming the field as the payload does (x_edges, not "x edges").
        assert (card["title"], card["section"]) == ("Posterior slice", "Search")
        assert card["data"] == {"form": "error", "message": f"Malformed map: {problem}"}
        # A well-formed neighbour is published as it always was.
        assert untouched["data"]["form"] == "heatmap"
        assert untouched["data"]["x_label"] == "Azimuth (dva)"

    def test_an_error_card_keeps_a_nan_out_of_the_published_state(self):
        # A NaN reaching the page's JSON would break the reading of the
        # whole update; replaced by the card, it never gets there.
        payload = search_slice()
        payload["maps"][0]["matrix"][1][1] = float("nan")
        state = live_monitor_state(
            revision=1,
            status="running",
            identity={},
            trials=[],
            events=[],
            spec=LiveMonitorSpec(include_defaults=False),
            extra_panels=[{"title": "Posterior slice", "data": payload}],
            on_invalid_panel=lambda title, problem: None,
        )
        assert state["panels"][0]["data"]["form"] == "error"
        json.dumps(state, allow_nan=False)

    def test_well_formed_heatmaps_old_and_new_are_published_and_serialise(self):
        state = self.publish(
            {"title": "Receptive fields", "data": rf_map()},
            {"title": "Posterior slice", "section": "Search", "data": search_slice()},
        )
        assert [panel["title"] for panel in state["panels"]] == [
            "Receptive fields",
            "Posterior slice",
        ]
        sent = state["panels"][1]["data"]
        assert (sent["x_scale"], sent["x_unit"], sent["vmin"]) == ("log", "dva/s", 0.30)
        json.dumps(state, allow_nan=False)

    def test_other_forms_are_not_held_to_the_heatmap_rules(self):
        # An "empty" placeholder carries no maps or edges at all.
        self.publish({"title": "RF", "data": {"form": "empty", "message": "waiting"}})
