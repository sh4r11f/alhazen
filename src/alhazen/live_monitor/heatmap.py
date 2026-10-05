"""The ``heatmap`` wire form: what a well-formed payload is.

A live analysis (``task/live.py``) sends its maps to the live monitor as
finished payloads that the page only draws — rf-mapping's receptive fields,
mbri's posterior slices. Nothing between the experiment that builds one and
the page that draws it looked inside, so a payload whose edges did not fit
its matrix reached the page as a card that only said "Malformed map", for
the rest of the session. :func:`check_heatmap` is the one place the Python
side says what such a payload must be.

It is strict on purpose, and experiments call it in their own tests: that is
where a payload's mistakes should fail. A running session does not stop for
one — ``live_monitor_state()`` runs this check on every heatmap it publishes,
and in a session (``SessionRunner``) a payload that fails it is drawn as an
error card naming the problem and logged once at ERROR, while the recording
goes on. The live monitor is a view of the data, not the data.

The form (docs/live_monitor.md, "The heatmap form"):

``maps``                 ``[{name, matrix, centroid?}]``; ``matrix[row][col]``,
                         row 0 the bottom row; a cell is a finite number or
                         ``None`` (not measured yet); every map the same shape
``x_edges``/``y_edges``  the cells' boundaries in real units, strictly
                         increasing, ``cols + 1`` / ``rows + 1`` of them
``x_scale``/``y_scale``  optional, both or neither: ``"linear"`` or ``"log"``
``x_unit``/``y_unit``    optional text written after a coordinate
``vmin``/``vmax``        the colour range; ``vmin`` optional (0 when absent),
                         needs ``vmax`` and lies below it

Only the shape of the payload is checked here, never its meaning: which
values are plausible is the experiment's business.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Real
from typing import Any

from alhazen.errors import SessionError

# The two ways an axis can map its values onto the drawing. The page draws a
# "log" axis at log10 of each value, so equal ratios take equal lengths.
SCALES = ("linear", "log")


def check_heatmap(data: Mapping[str, Any]) -> None:
    """Raise :class:`~alhazen.errors.SessionError` unless ``data`` is a
    well-formed heatmap payload.

    The message names the field and what is wrong with it. Call it in an
    experiment's tests on the payloads its live analysis builds: a session
    only reports a malformed one (see the module docstring). A payload whose
    ``maps`` hold no cells yet (an empty list, or empty matrices) is valid —
    the page draws "No map yet" — and then the edges' lengths are not
    checked, since there is nothing to check them against.
    """
    rows, cols = _maps_shape(data.get("maps"))
    scales = _scales(data)
    # x runs across the columns, y up the rows: matrix[row][col].
    _edges(data, "x", cols, scales.get("x"))
    _edges(data, "y", rows, scales.get("y"))
    for axis in ("x", "y"):
        unit = data.get(f"{axis}_unit")
        if unit is not None and not isinstance(unit, str):
            raise SessionError(f"heatmap {axis}_unit must be text such as 'dva/s', got {unit!r}")
    _colour_range(data)


def _is_number(value: Any) -> bool:
    """A real number as JSON carries one. ``True`` is an ``int`` to Python,
    but a cell holding a boolean is a bug in the payload, not a rate of 1."""
    return isinstance(value, Real) and not isinstance(value, bool)


def _is_list(value: Any) -> bool:
    """A JSON array as Python builds one: a list or a tuple, never a string
    (which would iterate as characters and pass as a row of them)."""
    return isinstance(value, (list, tuple))


def _maps_shape(maps: Any) -> tuple[int | None, int | None]:
    """``(rows, cols)`` shared by every map with cells, or ``(None, None)``
    when no map has a cell yet."""
    if maps is None:
        return None, None
    if not _is_list(maps):
        raise SessionError(f"heatmap maps must be a list of maps, got {type(maps).__name__}")
    shape: tuple[int, int] | None = None
    first = 0
    for index, one in enumerate(maps):
        if not isinstance(one, Mapping) or not _is_list(one.get("matrix")):
            raise SessionError(
                f"heatmap maps[{index}] must be a dict with a 'matrix' list of rows, "
                f"got {one!r:.80}"
            )
        matrix = one["matrix"]
        if not matrix:
            # No cells yet: the page draws such a map as "No map yet".
            continue
        widths = set()
        for row_index, row in enumerate(matrix):
            if not _is_list(row) or not row:
                raise SessionError(
                    f"heatmap maps[{index}] row {row_index} must be a non-empty list of "
                    f"cells, got {row!r:.80}"
                )
            widths.add(len(row))
            for col_index, cell in enumerate(row):
                # NaN and infinity are refused as well as non-numbers: JSON
                # has neither, and one NaN in a published state breaks the
                # live page's parsing of the whole update, not just this map.
                if cell is not None and not (_is_number(cell) and math.isfinite(cell)):
                    raise SessionError(
                        f"heatmap maps[{index}] cell [{row_index}][{col_index}] is {cell!r}; "
                        f"a cell is a finite number, or None for a cell not measured yet"
                    )
        if len(widths) > 1:
            raise SessionError(
                f"heatmap maps[{index}] has rows of different lengths {sorted(widths)}; "
                f"a map is a rectangle of cells"
            )
        this = (len(matrix), len(matrix[0]))
        if shape is None:
            shape, first = this, index
        elif this != shape:
            # One set of edges serves every map, so a map of another shape
            # would be drawn against edges that are not its own.
            raise SessionError(
                f"heatmap maps[{index}] is {this[0]}x{this[1]} cells but maps[{first}] is "
                f"{shape[0]}x{shape[1]}; every map shares one set of edges, so every map "
                f"must be the same shape"
            )
    return shape if shape is not None else (None, None)


def _scales(data: Mapping[str, Any]) -> dict[str, str]:
    """The axes' scales, ``{}`` when the payload draws no axes."""
    given = {axis: data.get(f"{axis}_scale") for axis in ("x", "y")}
    present = {axis: scale for axis, scale in given.items() if scale is not None}
    if len(present) == 1:
        # Giving a scale is what draws the axes. Half a pair is almost
        # certainly a forgotten line, and guessing "linear" for the other
        # axis would draw a log map as a linear one without a word.
        axis = next(iter(present))
        other = "y" if axis == "x" else "x"
        raise SessionError(
            f"heatmap gives {axis}_scale but not {other}_scale; giving them draws the "
            f"axes, so give both ('linear' or 'log')"
        )
    for axis, scale in present.items():
        if scale not in SCALES:
            raise SessionError(
                f"heatmap {axis}_scale is {scale!r}; it must be one of "
                f"{', '.join(repr(name) for name in SCALES)}"
            )
    return present


def _edges(data: Mapping[str, Any], axis: str, cells: int | None, scale: str | None) -> None:
    """One axis's edges: finite, strictly increasing, one more than the
    cells along that axis, and above 0 on a log axis."""
    key = f"{axis}_edges"
    across = "wide" if axis == "x" else "tall"
    edges = data.get(key)
    if edges is None:
        if cells is None:
            return
        raise SessionError(
            f"heatmap {key} is missing; the matrix is {cells} cell(s) {across}, so it needs "
            f"{cells + 1} edges"
        )
    if not _is_list(edges):
        raise SessionError(f"heatmap {key} must be a list of numbers, got {edges!r:.80}")
    for index, edge in enumerate(edges):
        if not _is_number(edge) or not math.isfinite(edge):
            raise SessionError(f"heatmap {key}[{index}] is {edge!r}; an edge is a finite number")
    for index in range(1, len(edges)):
        if not edges[index] > edges[index - 1]:
            raise SessionError(
                f"heatmap {key} must be strictly increasing, but {key}[{index}] = "
                f"{edges[index]:g} follows {edges[index - 1]:g}"
            )
    if cells is not None and len(edges) != cells + 1:
        raise SessionError(
            f"heatmap {key} has {len(edges)} values, but the matrix is {cells} cell(s) "
            f"{across}: it needs {cells + 1}, one more than the cells"
        )
    if scale == "log" and edges and edges[0] <= 0:
        # log10 of 0 or less is not a place on the axis.
        raise SessionError(
            f"heatmap {key} starts at {edges[0]:g}, but {axis}_scale is 'log': every edge "
            f"of a log axis must be above 0"
        )


def _colour_range(data: Mapping[str, Any]) -> None:
    """``vmin``/``vmax``: finite numbers, and ``vmin`` below ``vmax``."""
    vmin, vmax = data.get("vmin"), data.get("vmax")
    for key, value in (("vmin", vmin), ("vmax", vmax)):
        if value is not None and not (_is_number(value) and math.isfinite(value)):
            raise SessionError(f"heatmap {key} must be a finite number, got {value!r}")
    if vmin is None:
        return
    if vmax is None:
        raise SessionError(
            "heatmap gives vmin but no vmax; the colour range needs its top as well as its bottom"
        )
    if vmin == vmax:
        # What a flat surface gives when its limits are its own min and max.
        raise SessionError(
            f"heatmap colour range is empty: vmin and vmax are both {vmin:g}; vmin must be "
            f"below vmax"
        )
    if not vmin < vmax:
        raise SessionError(
            f"heatmap colour range is the wrong way round: vmin {vmin:g} is not below vmax {vmax:g}"
        )
