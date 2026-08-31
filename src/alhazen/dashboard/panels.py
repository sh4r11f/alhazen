"""What each dashboard panel actually plots, computed in Python.

The browser page is a *renderer*, not an analysis. Every count, bin edge,
mean, error bar, confidence interval and running proportion the dashboard
shows is computed here and travels to the page ready to draw. Two reasons,
both practical:

* **It is testable.** A statistic computed in the page's JavaScript has no
  test in this suite, and a running accuracy that divides by the wrong
  denominator looks exactly like one that doesn't.
* **It is bounded.** Every series is thinned to at most :data:`MAX_POINTS`
  before it is sent — more points than a panel has pixels tell the reader
  nothing — so a 5000-trial session publishes a panel the same size as a
  50-trial one.

Cost: one publish walks the session's trials and events once per panel. That
is the same order as the copy of those two lists the runner already makes for
every update, and far cheaper than serialising them.

The wire shape. Every payload is a dict with a ``form`` key naming the mark
the page must draw, plus the fields that form needs:

``empty``      ``message``
``stat``       ``value`` ``unit`` ``label`` ``secondary``
``bars``       ``items[{label,value,share}]`` ``total`` ``value_label``
``histogram``  ``bins[{x0,x1,count}]`` ``median`` ``x_label`` ``y_label``
``line``       ``series[{name,points,slot,line,step,marker}]`` ``band``
               ``marks`` ``x_label`` ``y_label`` ``y_domain``
``scatter``    ``series[{name,points,slot|ramp}]`` ``targets`` ``centroid``
               ``x_label`` ``y_label``
``vectors``    ``series`` (displacements) ``rings`` ``radius`` ``x_label``
               ``y_label``
``dots``       ``groups[{label,mean,sem|low/high,n}]`` ``style`` ``x_label``
               ``y_label``

Any payload may also carry ``stats`` (a small strip of labelled numbers shown
above the plot) and ``note`` (one line of caveat under it). ``slot`` is an
index into the page's categorical palette; colour is never chosen here.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

from alhazen.dashboard.spec import DashboardPanel

# More points than this in one series cannot be resolved on a dashboard panel
# a few hundred pixels wide, so sending them only costs serialisation time.
MAX_POINTS = 180

# Above this many classes a bar chart stops being readable and the tail is
# folded into one "Other" bar (the counts are still exact in the table view).
MAX_CLASSES = 7

# 95% normal quantile, for the Wilson interval on running proportions.
_Z95 = 1.959964

# Column names in this framework carry their unit as a suffix — ``rt_ms``,
# ``endpoint_x_dva``. Reading it off the name is what lets every axis be
# labelled with a unit without every task having to say so.
_UNIT_SUFFIXES = {
    "ms": "ms",
    "s": "s",
    "us": "µs",
    "hz": "Hz",
    "dva": "dva",
    "deg": "deg",
    "px": "px",
    "mm": "mm",
    "cm": "cm",
    "ul": "µL",
    "ml": "mL",
    "pct": "%",
    "v": "V",
}


# ----------------------------------------------------------------------
# Small numeric helpers
# ----------------------------------------------------------------------


def _num(value: Any) -> bool:
    """True for a real, finite number. ``bool`` is excluded deliberately: it
    is an ``int`` subclass, so without this every True/False column would be
    plotted as 1/0 on a numeric axis."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _numbers(rows: list[dict[str, Any]], field: str | None) -> list[float]:
    if not field:
        return []
    return [float(row[field]) for row in rows if _num(row.get(field))]


def _quantile(ordered: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    if not ordered:
        return math.nan
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else math.nan


def _sd(values: list[float]) -> float:
    """Sample standard deviation; NaN below two observations, never 0.0 — a
    spread that was never measurable must not be drawn as a zero-width one."""
    if len(values) < 2:
        return math.nan
    mean = _mean(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _nice_step(raw: float) -> float:
    """Round a raw interval up to the nearest 1, 2, 2.5 or 5 times a power of
    ten, so histogram edges and axis ticks land on numbers a human reads."""
    if not math.isfinite(raw) or raw <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw))
    for candidate in (1, 2, 2.5, 5, 10):
        if raw <= candidate * magnitude:
            return candidate * magnitude
    return 10 * magnitude


def _thin_indices(count: int, cap: int = MAX_POINTS) -> list[int]:
    """Evenly spaced indices into a series of ``count`` points, always
    including the first and the last. Thinning a monotone or slowly varying
    series this way is invisible at panel width; sending every point is not
    free."""
    if count <= cap:
        return list(range(count))
    step = (count - 1) / (cap - 1)
    return sorted({min(count - 1, round(i * step)) for i in range(cap)})


def _wilson(successes: int, total: int) -> tuple[float, float]:
    """95% Wilson score interval for a proportion.

    Wilson rather than the textbook normal interval because a dashboard shows
    proportions computed from a handful of trials, where the normal interval
    is badly wrong (and can run past 0 or 1) exactly when the experimenter is
    most tempted to read it.
    """
    if total <= 0:
        return (0.0, 1.0)
    p = successes / total
    denominator = 1 + _Z95**2 / total
    centre = (p + _Z95**2 / (2 * total)) / denominator
    spread = _Z95 * math.sqrt(p * (1 - p) / total + _Z95**2 / (4 * total**2)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def format_number(value: float) -> str:
    """A number with as many decimals as it deserves and no more."""
    if not math.isfinite(value):
        return "—"
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.0f}"
    if magnitude >= 100:
        return f"{value:.0f}"
    if magnitude >= 10:
        return f"{value:.1f}"
    if magnitude >= 1:
        return f"{value:.2f}"
    if magnitude == 0:
        return "0"
    return f"{value:.3g}"


def split_unit(field: str) -> tuple[str, str | None]:
    """``'rt_ms'`` -> ``('rt', 'ms')``; ``'response_key'`` -> unchanged."""
    parts = field.split("_")
    if len(parts) > 1 and parts[-1].lower() in _UNIT_SUFFIXES:
        return " ".join(parts[:-1]), _UNIT_SUFFIXES[parts[-1].lower()]
    return field.replace("_", " "), None


def axis_label(field: str | None, unit: str | None = None) -> str:
    """The text under (or beside) an axis, unit included when one is known."""
    if not field:
        return ""
    name, detected = split_unit(field)
    shown = unit or detected
    return f"{name} ({shown})" if shown else name


# ----------------------------------------------------------------------
# Row selection
# ----------------------------------------------------------------------


def _required(panel: DashboardPanel, field: str) -> str:
    """The panel field this kind cannot be drawn without.

    ``DashboardPanel`` validates these at construction, so reaching the raise
    means a panel was built past its own validator. Loud, because the
    alternative is a permanently and inexplicably empty plot.
    """
    value = getattr(panel, field, None)
    if not value:
        raise ValueError(f"{panel.kind} panel {panel.title!r} has no {field}")
    return str(value)


def select_rows(panel: DashboardPanel, trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The trials one panel reads, after its two declared filters.

    ``completed_only`` uses the row's own ``completed`` column — the engine
    stamps it from the outcome — so an experiment's own incomplete outcome is
    excluded whatever the experiment happens to call it.

    ``where`` compares as text, so a level written as ``"far"`` and one written
    as ``8.25`` are both matched by what the record shows. A missing column
    matches nothing, which leaves the panel empty and captioned rather than
    silently showing every trial.
    """
    rows = list(trials)
    if panel.completed_only:
        rows = [row for row in rows if row.get("completed") is True]
    for column, wanted in (panel.where or {}).items():
        rows = [row for row in rows if str(row.get(column)) == wanted]
    if panel.rolling_window:
        rows = rows[-panel.rolling_window :]
    return rows


def _x_values(rows: list[dict[str, Any]]) -> list[float]:
    """The x position of each row on a trial axis.

    ``trial_index`` counts every attempt and never resets, so it is what the
    log, the CSV and the experimenter all mean by "trial 47". Rows without one
    fall back to their position, which keeps a hand-built record plottable.
    """
    return [
        float(row["trial_index"]) if _num(row.get("trial_index")) else float(i + 1)
        for i, row in enumerate(rows)
    ]


# ----------------------------------------------------------------------
# Panel payloads, one per kind
# ----------------------------------------------------------------------


# An ordinal ramp has this many documented steps; past it a level would need
# a colour that was never validated against the surface it is drawn on.
MAX_RAMP_LEVELS = 5

# Scatter and vector plots put any two colours side by side, which is a harder
# separation test than a bar chart's neighbours. Three is what the palette
# clears there; the rest fold into one grey "other".
MAX_SPATIAL_SERIES = 3


def _level_order(labels: Iterable[str]) -> tuple[list[str], bool]:
    """The levels of a condition column, ordered, and whether they are numeric.

    Numeric levels are *ordinal* — 0.05 really is less than 0.4 — so they take
    one hue light-to-dark and the reader sees the order in the colour. Named
    levels ("left", "right") have no order to show, so they take separate hues.
    """
    unique = sorted(set(labels))
    try:
        return sorted(unique, key=float), True
    except ValueError:
        return unique, False


def _colour_series(
    rows: list[dict[str, Any]],
    field: str | None,
    point_of: Callable[[dict[str, Any]], list[float] | None],
) -> tuple[list[dict[str, Any]], str | None]:
    """Split rows into drawable series, coloured by a condition column.

    Returns the series and a note when levels had to be folded — a colour
    beyond the validated set would be one the reader cannot reliably tell from
    another, and inventing one is how a legend starts lying.
    """
    points_by_level: dict[str, list[list[float]]] = {}
    unlabelled: list[list[float]] = []
    for row in rows:
        point = point_of(row)
        if point is None:
            continue
        if not field:
            points_by_level.setdefault("", []).append(point)
        elif row.get(field) is None:
            # A trial the experiment never labelled. Kept and shown, because
            # it happened — but held out of the levels, so one unlabelled
            # trial cannot turn an ordered factor into an unordered one.
            unlabelled.append(point)
        else:
            points_by_level.setdefault(str(row[field]), []).append(point)
    if not points_by_level and not unlabelled:
        return [], None
    if not field:
        one = points_by_level[""]
        return [{"name": "", "slot": 1, "points": _thin(one), "centroid": _centroid(one)}], None

    levels, ordinal = _level_order(points_by_level)
    cap = MAX_RAMP_LEVELS if ordinal else MAX_SPATIAL_SERIES
    shown, folded = levels[:cap], levels[cap:]
    extras = bool(folded) + bool(unlabelled)
    budget = max(24, MAX_POINTS // max(1, len(shown) + extras))

    series: list[dict[str, Any]] = []
    for index, level in enumerate(shown):
        entry: dict[str, Any] = {
            "name": level,
            "points": _thin(points_by_level[level], budget),
            # From every point of this level, not the thinned sample, and per
            # level rather than over the lot: one mean of a left cluster and a
            # right one lands between them, where nothing did.
            "centroid": _centroid(points_by_level[level]),
        }
        if ordinal:
            # Spread the ramp over however many levels there are, so two
            # levels are light-and-dark rather than two adjacent steps.
            entry["ramp"] = (
                round(index * (MAX_RAMP_LEVELS - 1) / max(1, len(shown) - 1))
                if len(shown) > 1
                else 2
            )
        else:
            entry["slot"] = index + 1
        series.append(entry)
    if folded:
        rest = [point for level in folded for point in points_by_level[level]]
        series.append(
            {
                "name": f"other ({len(folded)} levels)",
                "muted": True,
                "points": _thin(rest, budget),
                "centroid": _centroid(rest),
            }
        )
    if unlabelled:
        series.append(
            {
                "name": f"{field} missing",
                "muted": True,
                "points": _thin(unlabelled, budget),
                "centroid": _centroid(unlabelled),
            }
        )
    note = None if not folded else f"{len(folded)} further {field} levels folded into one colour"
    return series, note


def _thin(points: list[list[float]], cap: int = MAX_POINTS) -> list[list[float]]:
    return [points[i] for i in _thin_indices(len(points), cap)]


def _centroid(points: list[list[float]]) -> list[float]:
    return [_mean([p[0] for p in points]), _mean([p[1] for p in points])]


def _empty(message: str) -> dict[str, Any]:
    return {"form": "empty", "message": message}


def _category_bars(labels: list[str], *, value_label: str, empty_message: str) -> dict[str, Any]:
    """Counts of a nominal column as horizontal bars.

    Horizontal because outcome and response names are words — ``BROKE_FIXATION``
    under a vertical bar either overlaps its neighbour or gets rotated, and both
    are worse than reading it left to right.
    """
    if not labels:
        return _empty(empty_message)
    counts = Counter(labels)
    total = sum(counts.values())
    ordered = counts.most_common()
    items = [
        {"label": name, "value": count, "share": count / total}
        for name, count in ordered[:MAX_CLASSES]
    ]
    tail = ordered[MAX_CLASSES:]
    if tail:
        # Folded rather than drawn: past ~7 classes adjacent bars stop being
        # tellable apart. The exact counts stay reachable in the table view.
        folded = sum(count for _, count in tail)
        items.append(
            {
                "label": f"Other ({len(tail)} more)",
                "value": folded,
                "share": folded / total,
            }
        )
    return {
        "form": "bars",
        "items": items,
        "total": total,
        "value_label": value_label,
        "stats": [{"label": "total", "value": f"{total:,}"}],
    }


def _outcomes(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [str(row["outcome"]) for row in rows if row.get("outcome") is not None]
    return _category_bars(labels, value_label="trials", empty_message="No trials recorded yet")


def _responses(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    # The only panel whose column has a sensible default: a task that records
    # keypresses at all records them under this name.
    field = panel.value or "response_key"
    labels = [str(row[field]) for row in rows if row.get(field) is not None]
    return _category_bars(labels, value_label="trials", empty_message=f"No {field} recorded yet")


def _histogram(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    field = _required(panel, "value")
    values = _numbers(rows, field)
    if not values:
        return _empty(f"No {field} recorded yet")
    ordered = sorted(values)
    n = len(ordered)
    median = _quantile(ordered, 0.5)
    q25, q75 = _quantile(ordered, 0.25), _quantile(ordered, 0.75)
    iqr = q75 - q25

    # A single four-second trial must not squeeze sixty real ones into one
    # bar. The axis is clipped to a robust window and the excluded tail is
    # counted in the note below — which is what a journal figure does. The
    # trials are never dropped silently, and n and the median (which is
    # robust) are still reported over everything.
    low, high = ordered[0], ordered[-1]
    if iqr > 0:
        low = max(low, q25 - 3 * iqr)
        high = min(high, q75 + 3 * iqr)
    inside = [value for value in ordered if low <= value <= high]
    outside = n - len(inside)

    if high == low:
        # Every observation identical: one bin centred on the value, rather
        # than a divide-by-zero or a fake spread.
        width = _nice_step(abs(low) / 10 or 1.0)
        edges = [low - width / 2, low + width / 2]
    else:
        # Freedman–Diaconis: bin width from the IQR, which is what makes a
        # histogram of reaction times robust to the one 4-second outlier that
        # would otherwise squeeze every real trial into a single bar. Sturges
        # is the fallback when the middle half of the data has no spread.
        raw = 2 * iqr * n ** (-1 / 3) if iqr > 0 else (high - low) / (math.log2(n) + 1)
        width = _nice_step(raw if raw > 0 else (high - low) / 10)
        # Bin count is clamped: too few hides the shape, too many turns the
        # histogram into a rug of single-trial spikes.
        while (high - low) / width > 30:
            width *= 2
        while (high - low) / width < 4:
            width /= 2
        start = math.floor(low / width) * width
        count = max(1, math.ceil((high - start) / width))
        edges = [start + i * width for i in range(count + 1)]

    counts = [0] * (len(edges) - 1)
    for value in inside:
        index = min(len(counts) - 1, max(0, int((value - edges[0]) // (edges[1] - edges[0]))))
        counts[index] += 1

    unit = panel.unit or split_unit(field)[1] or ""
    suffix = f" {unit}" if unit else ""
    payload: dict[str, Any] = {
        "form": "histogram",
        "bins": [
            {"x0": edges[i], "x1": edges[i + 1], "count": counts[i]} for i in range(len(counts))
        ],
        "median": median,
        "x_label": axis_label(field, panel.unit),
        "y_label": "trials",
        "stats": [
            {"label": "n", "value": f"{n:,}"},
            {"label": "median", "value": f"{format_number(median)}{suffix}"},
            {"label": "IQR", "value": f"{format_number(iqr)}{suffix}"},
        ],
    }
    if outside:
        # Both ends of the range, because the window is clipped at both: a
        # note that only ever named the maximum would hide a floor artefact.
        plural = "s" if outside > 1 else ""
        span = f"{format_number(ordered[0])}–{format_number(ordered[-1])}{suffix}"
        payload["note"] = f"{outside} trial{plural} outside the axis (full range {span})"
    return payload


def _scatter(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    x_field = _required(panel, "x")
    y_field = _required(panel, "y")

    def landing(row: dict[str, Any]) -> list[float] | None:
        if not (_num(row.get(x_field)) and _num(row.get(y_field))):
            return None
        return [float(row[x_field]), float(row[y_field])]

    points = [point for row in rows if (point := landing(row)) is not None]
    if not points:
        return _empty(f"No {x_field}/{y_field} recorded yet")
    series, fold_note = _colour_series(rows, panel.color_by, landing)

    targets: list[list[float]] = []
    errors: list[float] = []
    if panel.target_x and panel.target_y:
        seen: set[tuple[float, float]] = set()
        for row in rows:
            if not (_num(row.get(panel.target_x)) and _num(row.get(panel.target_y))):
                continue
            target = (float(row[panel.target_x]), float(row[panel.target_y]))
            if target not in seen:
                seen.add(target)
                targets.append([target[0], target[1]])
            if _num(row.get(x_field)) and _num(row.get(y_field)):
                errors.append(
                    math.hypot(float(row[x_field]) - target[0], float(row[y_field]) - target[1])
                )

    unit = panel.unit or split_unit(x_field)[1] or ""
    suffix = f" {unit}" if unit else ""
    stats = [{"label": "n", "value": f"{len(points):,}"}]
    if errors:
        # The number an experimenter actually wants off a landing plot: how far
        # the response fell from where it was asked to go.
        median_error = format_number(_quantile(sorted(errors), 0.5))
        stats.append({"label": "median error", "value": f"{median_error}{suffix}"})
    if len(targets) > 1:
        # A mean landing is only a landing when there is one place to land.
        # Across two targets it falls between the clusters whatever the points
        # are grouped by — including by a condition that has nothing to do
        # with where the target was.
        for entry in series:
            entry.pop("centroid", None)

    payload: dict[str, Any] = {
        "form": "scatter",
        "series": series,
        "targets": targets[:MAX_CLASSES],
        "x_label": axis_label(x_field, panel.unit),
        "y_label": axis_label(y_field, panel.unit),
        # Landings live in a real 2-D space: a millimetre right must be the
        # same length on screen as a millimetre up, or the scatter's shape is
        # a lie about where the subject looked.
        "equal_aspect": True,
        "stats": stats,
        "color_label": panel.color_by,
    }
    if fold_note:
        payload["note"] = fold_note
    return payload


def _vectors(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every response as a displacement from where the eye started.

    The landing panel answers "did it hit the target". This one answers "how
    far, and which way, did it go" — every trial collapsed onto one origin, so
    amplitude and direction stay readable even when the fixation point moves
    between trials. Drawn on a polar grid (rings of constant amplitude)
    because that is what the two questions are: a radius and an angle.
    """
    x_field = _required(panel, "x")
    y_field = _required(panel, "y")
    origin_x, origin_y = panel.origin_x, panel.origin_y
    # A task whose fixation point never moves does not write a column for it.
    # Screen centre is where this framework's fixation point sits, so that is
    # the fallback — stated in a note, never assumed silently.
    assumed = not (
        origin_x
        and origin_y
        and any(_num(row.get(origin_x)) and _num(row.get(origin_y)) for row in rows)
    )

    def displacement(row: dict[str, Any]) -> list[float] | None:
        if not (_num(row.get(x_field)) and _num(row.get(y_field))):
            return None
        if assumed:
            start_x = start_y = 0.0
        elif origin_x and origin_y and _num(row.get(origin_x)) and _num(row.get(origin_y)):
            start_x, start_y = float(row[origin_x]), float(row[origin_y])
        else:
            # This trial's own origin is missing: its displacement is unknown,
            # and plotting it against somebody else's origin would be a guess.
            return None
        return [float(row[x_field]) - start_x, float(row[y_field]) - start_y]

    points = [point for row in rows if (point := displacement(row)) is not None]
    if not points:
        return _empty(f"No {x_field}/{y_field} recorded yet")
    series, fold_note = _colour_series(rows, panel.color_by, displacement)

    amplitudes = sorted(math.hypot(dx, dy) for dx, dy in points)
    radius = amplitudes[-1] * 1.12 or 1.0
    # Amplitude rings are this plot's gridlines, so they land on the same
    # readable numbers a linear axis would.
    step = _nice_step(radius / 5)
    rings = [step * i for i in range(1, min(6, int(radius / step)) + 1)]

    unit = panel.unit or split_unit(x_field)[1] or ""
    suffix = f" {unit}" if unit else ""
    payload: dict[str, Any] = {
        "form": "vectors",
        "series": series,
        "rings": rings,
        "radius": radius,
        "x_label": f"horizontal displacement{f' ({unit})' if unit else ''}",
        "y_label": f"vertical displacement{f' ({unit})' if unit else ''}",
        "origin_label": "fixation",
        "stats": [
            {"label": "n", "value": f"{len(points):,}"},
            {
                "label": "median amplitude",
                "value": f"{format_number(_quantile(amplitudes, 0.5))}{suffix}",
            },
            {
                "label": "IQR",
                "value": (
                    f"{format_number(_quantile(amplitudes, 0.75) - _quantile(amplitudes, 0.25))}"
                    f"{suffix}"
                ),
            },
        ],
    }
    payload["color_label"] = panel.color_by
    notes = []
    if assumed:
        named = f" ({origin_x} is not recorded)" if origin_x else ""
        notes.append(f"origin assumed at screen centre{named}")
    if fold_note:
        notes.append(fold_note)
    if notes:
        payload["note"] = " · ".join(notes)
    return payload


def _moving_window(n: int) -> int:
    """A smoothing window that scales with the session: wide enough to be a
    trend, narrow enough to still move within a block of trials."""
    return max(5, min(51, round(n / 10) or 5))


def _series(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    field = _required(panel, "value")
    xs = _x_values(rows)
    pairs = [[xs[i], float(row[field])] for i, row in enumerate(rows) if _num(row.get(field))]
    if not pairs:
        return _empty(f"No {field} recorded yet")

    series: list[dict[str, Any]] = [
        {
            "name": axis_label(field, panel.unit),
            "slot": 1,
            "points": [pairs[i] for i in _thin_indices(len(pairs))],
            "line": False,
            "marker": True,
        }
    ]
    if len(pairs) >= 12:
        # The raw trace of a per-trial measurement is mostly trial-to-trial
        # noise. The moving mean is the part an experimenter is reading it for,
        # so it is drawn on top rather than left for the eye to fit.
        window = _moving_window(len(pairs))
        smoothed = []
        for i in range(len(pairs)):
            chunk = [p[1] for p in pairs[max(0, i - window + 1) : i + 1]]
            smoothed.append([pairs[i][0], _mean(chunk)])
        series.append(
            {
                "name": f"moving mean ({window} trials)",
                "slot": 2,
                "points": [smoothed[i] for i in _thin_indices(len(smoothed))],
                "line": True,
                "marker": False,
            }
        )

    values = [p[1] for p in pairs]
    unit = panel.unit or split_unit(field)[1] or ""
    suffix = f" {unit}" if unit else ""
    return {
        "form": "line",
        "series": series,
        "x_label": "trial",
        "y_label": axis_label(field, panel.unit),
        "stats": [
            {"label": "n", "value": f"{len(values):,}"},
            {"label": "mean", "value": f"{format_number(_mean(values))}{suffix}"},
        ],
    }


def _group_order(label: str) -> tuple[int, float, str]:
    """Numeric condition levels sort as numbers, not as strings.

    The string order "0.2" < "0.4" < "10" happens to be right until a level
    reaches double digits, which is exactly when nobody is looking.
    """
    try:
        return (0, float(label), "")
    except ValueError:
        return (1, 0.0, label)


def _grouped_mean(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    field = _required(panel, "value")
    fields = panel.group_fields
    if not fields:
        raise ValueError("grouped_mean panels require group")

    # Keyed by (factor, level) rather than by level alone. Two factors can name
    # the same level — `near` under separation and `near` under anything else —
    # and merging them would silently average unrelated trials together.
    buckets: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        if not _num(row.get(field)):
            continue
        for group_field in fields:
            key = row.get(group_field)
            if key is not None:
                buckets.setdefault((group_field, str(key)), []).append(float(row[field]))
    if not buckets:
        return _empty(f"No {field} by {' / '.join(fields)} yet")

    groups: list[dict[str, Any]] = []
    trials_shown = 0
    # Factors in the order they were declared, levels ordered within each, so
    # the bars of one factor stay together and the reader compares within a
    # colour before comparing across.
    ordered = sorted(buckets, key=lambda k: (fields.index(k[0]), _group_order(k[1])))
    for group_field, label in ordered:
        values = buckets[(group_field, label)]
        trials_shown += len(values)
        sd = _sd(values)
        groups.append(
            {
                "label": label,
                "series": group_field.replace("_", " "),
                "mean": _mean(values),
                # Standard error of the mean: the error bar answers "how well
                # is this mean pinned down", which is the question a group
                # comparison asks. NaN travels as None for a single trial,
                # where no spread was measured — better a bare dot than a
                # zero-length bar implying certainty.
                "sem": None if math.isnan(sd) else sd / math.sqrt(len(values)),
                "n": len(values),
            }
        )
    # One factor is a plain grouped panel and keeps its own axis label; several
    # share one axis, and the label that matters is then on the legend.
    return {
        "form": "dots",
        "groups": groups,
        "style": panel.style or "dots",
        "x_label": axis_label(fields[0]) if len(fields) == 1 else "condition",
        "y_label": axis_label(field, panel.unit),
        "error_label": "± SEM",
        "stats": [
            {"label": "groups", "value": str(len(groups))},
            # `n` counts placements, not trials: with several factors every
            # trial appears once per factor, and saying "trials" would overcount
            # the session by exactly that multiple.
            {"label": "n", "value": f"{trials_shown:,}"},
        ],
    }


def _score(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[bool], str, str]:
    """What "doing well" means for this session, and which trials say so.

    Accuracy when the task scores its outcomes, completion rate when it does
    not. The fallback is named on the axis rather than assumed: a task with no
    notion of "correct" still has a subject that either finishes trials or
    breaks them, and that is the curve worth watching. Shared so the running
    figure and the by-condition figure can never disagree about it.
    """
    scored = [row for row in rows if isinstance(row.get("success"), bool)]
    if scored:
        return scored, [bool(row["success"]) for row in scored], "proportion correct", "accuracy"
    return (
        rows,
        [row.get("completed") is True for row in rows],
        "proportion completed",
        "completion",
    )


def _grouped_rate(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Proportion correct per condition level, with a 95% interval.

    Bars rather than dots: a proportion is measured from zero, so the length
    of the bar is the quantity. The interval is Wilson's, and it is asymmetric
    near 0 and 1 — which is exactly where a handful of trials in one level
    puts it, and where a symmetric bar would overstate what is known.
    """
    # `group_fields` rather than `panel.group`: the field accepts a sequence
    # now, and the spec already refuses more than one of them here.
    group_field = panel.group_fields[0]
    grouped = [row for row in rows if row.get(group_field) is not None]
    source, hits, y_label, _ = _score(grouped)
    if not source:
        return _empty(f"No scored trials by {group_field} yet")

    buckets: dict[str, list[bool]] = {}
    for row, hit in zip(source, hits, strict=True):
        buckets.setdefault(str(row[group_field]), []).append(hit)

    groups = []
    for label in sorted(buckets, key=_group_order):
        outcomes = buckets[label]
        successes = sum(outcomes)
        low, high = _wilson(successes, len(outcomes))
        groups.append(
            {
                "label": label,
                "mean": successes / len(outcomes),
                "low": low,
                "high": high,
                "n": len(outcomes),
            }
        )
    return {
        "form": "dots",
        "groups": groups,
        "style": panel.style or "bars",
        "x_label": axis_label(group_field),
        "y_label": y_label,
        # A proportion axis is 0 to 1. Auto-scaling one level's 0.62 against
        # another's 0.68 turns noise into a result.
        "y_domain": [0.0, 1.0],
        "error_label": "95% CI",
        "stats": [
            {"label": "levels", "value": str(len(groups))},
            {"label": "n", "value": f"{len(source):,}"},
        ],
    }


def _performance(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A running proportion over trials — the panel a session is watched on.

    Accuracy when the task scores its outcomes, completion rate when it does
    not. The fallback is stated on the axis rather than assumed: a task with
    no notion of "correct" still has a subject that either finishes trials or
    breaks them, and that curve is the one worth watching.
    """
    source, hits, y_label, stat_label = _score(rows)
    if not source:
        return _empty("No trials recorded yet")

    xs = _x_values(source)
    cumulative: list[list[float]] = []
    band: list[list[float]] = []
    successes = 0
    for i, hit in enumerate(hits):
        successes += hit
        n = i + 1
        low, high = _wilson(successes, n)
        cumulative.append([xs[i], successes / n])
        band.append([xs[i], low, high])

    keep = _thin_indices(len(cumulative))
    series: list[dict[str, Any]] = [
        {
            "name": "cumulative",
            "slot": 1,
            "points": [cumulative[i] for i in keep],
            "line": True,
            "marker": False,
        }
    ]
    if len(hits) >= 12:
        window = _moving_window(len(hits))
        recent = []
        for i in range(len(hits)):
            chunk = hits[max(0, i - window + 1) : i + 1]
            recent.append([xs[i], sum(chunk) / len(chunk)])
        series.append(
            {
                "name": f"last {window} trials",
                "slot": 2,
                "points": [recent[i] for i in _thin_indices(len(recent))],
                "line": True,
                "marker": False,
            }
        )

    final_low, final_high = _wilson(successes, len(hits))
    return {
        "form": "line",
        "series": series,
        "band": {"slot": 1, "name": "95% CI", "points": [band[i] for i in keep]},
        "x_label": "trial",
        "y_label": y_label,
        # Pinned to the full range: an accuracy axis auto-scaled to
        # 0.62–0.68 turns noise into a dramatic-looking climb.
        "y_domain": [0.0, 1.0],
        "stats": [
            {"label": stat_label, "value": f"{100 * successes / len(hits):.1f}%"},
            {"label": "95% CI", "value": f"{100 * final_low:.0f}–{100 * final_high:.0f}%"},
            {"label": "n", "value": f"{len(hits):,}"},
        ],
    }


def _reward_deliveries(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk the event stream once and pull out everything reward-related."""
    delivered: list[tuple[float, float, bool]] = []  # (trial, open_ms, manual)
    failures: list[float] = []
    declined = 0
    unpriced = 0
    last_trial = 0.0
    for event in events:
        trial = float(event["trial_index"]) if _num(event.get("trial_index")) else 0.0
        last_trial = max(last_trial, trial)
        name = event.get("event")
        if name not in {"REWARD", "REWARD_FAILED", "NO_REWARD"}:
            continue
        if name == "NO_REWARD":
            declined += 1
            continue
        if name == "REWARD_FAILED":
            failures.append(trial)
            continue
        try:
            payload = json.loads(event.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        pulses = payload.get("pulses") or {}
        if _num(pulses.get("n_pulses")) and _num(pulses.get("pulse_ms")):
            open_ms = float(pulses["n_pulses"]) * float(pulses["pulse_ms"])
        else:
            # A delivery whose pulse train was not recorded cannot be priced.
            # Counted, never guessed — the panel says so and falls back to
            # counting deliveries rather than reporting a volume it invented.
            open_ms = math.nan
            unpriced += 1
        delivered.append((trial, open_ms, bool(payload.get("manual"))))
    return {
        "delivered": delivered,
        "failures": failures,
        "declined": declined,
        "unpriced": unpriced,
        "last_trial": last_trial,
    }


def _rewards(panel: DashboardPanel, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Cumulative reward as a function of trial.

    A session's total reward is one number and is shown as one number. The
    *curve* is the thing worth a plot: it says when the subject earned, how
    fast, and — by going flat — exactly when it stopped working. Trials do not
    all pay the same, so the steps are uneven and that unevenness is the
    signal.
    """
    found = _reward_deliveries(events)
    delivered = found["delivered"]
    if not delivered and not found["failures"]:
        return _empty("No reward delivered yet")

    priced = found["unpriced"] == 0 and bool(delivered)
    if priced:
        # Valve-open time, not millilitres: volume per pulse is a property of
        # the pump's calibration, which alhazen does not know. Open time is
        # exactly proportional to it, so the shape is right and the axis is
        # honest about what was measured.
        y_label = "cumulative valve-open time (s)"
        weights = [open_ms / 1000.0 for _, open_ms, _ in delivered]
    else:
        y_label = "cumulative deliveries"
        weights = [1.0 for _ in delivered]

    start = min([trial for trial, _, _ in delivered] + found["failures"] + [found["last_trial"]])
    points: list[list[float]] = [[max(0.0, start - 1), 0.0]]
    running = 0.0
    for (trial, _, _), weight in zip(delivered, weights, strict=True):
        running += weight
        points.append([trial, running])
    if found["last_trial"] > points[-1][0]:
        # Carry the curve to the present trial. Without this the line stops at
        # the last delivery and a subject that quit ten minutes ago looks like
        # a subject still earning.
        points.append([found["last_trial"], running])

    manual = sum(1 for _, _, is_manual in delivered if is_manual)
    stats = [
        {
            "label": "total",
            "value": f"{format_number(running)} s" if priced else f"{format_number(running)}",
        },
        {"label": "deliveries", "value": f"{len(delivered):,}"},
    ]
    if manual:
        stats.append({"label": "manual", "value": f"{manual:,}"})
    if found["declined"]:
        stats.append({"label": "unrewarded", "value": f"{found['declined']:,}"})
    if found["failures"]:
        stats.append(
            {"label": "failed", "value": f"{len(found['failures']):,}", "status": "critical"}
        )

    keep = _thin_indices(len(points))
    payload: dict[str, Any] = {
        "form": "line",
        "series": [
            {
                "name": "reward delivered",
                "slot": 1,
                "points": [points[i] for i in keep],
                "line": True,
                "marker": False,
                # A cumulative total does not drift between deliveries; it
                # jumps at one. A straight interpolation would draw reward the
                # subject never got.
                "step": True,
            }
        ],
        "marks": [
            {"x": trial, "y": _cumulative_at(points, trial), "kind": "failure"}
            for trial in found["failures"][-MAX_POINTS:]
        ],
        "x_label": "trial",
        "y_label": y_label,
        "stats": stats,
    }
    if not priced:
        payload["note"] = (
            f"{found['unpriced']} of {len(delivered)} deliveries carry no pulse train, "
            "so this counts deliveries instead of valve-open time"
        )
    return payload


def _cumulative_at(points: list[list[float]], x: float) -> float:
    """The running total at trial ``x`` — where a failure mark sits."""
    total = 0.0
    for point_x, point_y in points:
        if point_x > x:
            break
        total = point_y
    return total


def _stat(panel: DashboardPanel, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One number, shown as one number.

    A single scalar drawn as a one-bar bar chart tells the reader nothing the
    digits don't, and spends a whole panel doing it.
    """
    field = _required(panel, "value")
    values = _numbers(rows, field)
    if panel.agg == "count":
        # Counting is the one aggregate that is meaningful over a non-numeric
        # column, so it counts populated rows rather than parsed numbers.
        populated = [row for row in rows if row.get(field) is not None]
        return {
            "form": "stat",
            "value": f"{len(populated):,}",
            "unit": "",
            "label": f"{split_unit(field)[0]} recorded",
            "secondary": f"of {len(rows):,} trials",
        }
    if not values:
        return _empty(f"No {field} recorded yet")

    reducers: dict[str, Callable[[list[float]], float]] = {
        "mean": _mean,
        "median": lambda v: _quantile(sorted(v), 0.5),
        "sum": lambda v: float(sum(v)),
        "min": min,
        "max": max,
        "last": lambda v: v[-1],
    }
    reduced = reducers[panel.agg](values)

    unit = panel.unit or split_unit(field)[1] or ""
    secondary = f"n = {len(values):,}"
    if panel.agg in {"mean", "median"} and len(values) > 1:
        sd = _sd(values)
        secondary += f" · SD {format_number(sd)}{f' {unit}' if unit else ''}"
    return {
        "form": "stat",
        "value": format_number(reduced),
        "unit": unit,
        "label": f"{panel.agg} {split_unit(field)[0]}",
        "secondary": secondary,
    }


_TRIAL_PANELS = {
    "outcomes": _outcomes,
    "responses": _responses,
    "histogram": _histogram,
    "scatter": _scatter,
    "vectors": _vectors,
    "series": _series,
    "grouped_mean": _grouped_mean,
    "grouped_rate": _grouped_rate,
    "performance": _performance,
    "stat": _stat,
}


def panel_payload(
    panel: DashboardPanel,
    trials: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Everything the page needs to draw one panel, and nothing else."""
    if panel.kind == "rewards":
        # The only panel drawn from the event stream: reward is delivered
        # between trials and on trials that never wrote a row.
        return _rewards(panel, events)
    build = _TRIAL_PANELS.get(panel.kind)
    if build is None:  # pragma: no cover - the Literal makes this unreachable
        raise ValueError(f"no renderer for dashboard panel kind {panel.kind!r}")
    payload = build(panel, select_rows(panel, trials))
    if panel.rolling_window and payload["form"] != "empty":
        # Appended, never assigned: a panel may already have something to say
        # (a histogram's clipped tail), and losing that to the window note
        # would be the quiet kind of wrong this module exists to avoid.
        window = f"most recent {panel.rolling_window} trials"
        existing = payload.get("note")
        payload["note"] = f"{window} · {existing}" if existing else window
    return payload
