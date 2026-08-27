"""Declarative plots understood by the live browser dashboard.

A panel declares *what* to plot — which columns of a completed trial record,
over which rows — and never how to draw it. The mark, the axes, the binning
and the error bars follow from ``kind`` and are chosen once, in
:mod:`alhazen.dashboard.panels`, so every task's dashboard is read the same
way and no experiment can invent a misleading chart by accident.

The kinds exist because each answers a different question:

``performance``   is the subject working? — a running proportion over trials
``rewards``       how much has it earned, and is that still accruing? —
                  a cumulative curve, because a total is not a shape
``outcomes``      how are attempts distributed across outcome names?
``responses``     which key/button is being pressed?
``histogram``     what does one measured quantity's distribution look like?
``scatter``       where in space did a response land?
``vectors``       how far, and in which direction, did the eye move? —
                  every trial collapsed onto one origin
``series``        how does one quantity drift over the session?
``grouped_mean``  does a quantity differ across a condition? (mean ± SEM)
``grouped_rate``  does the subject do better in one condition than
                  another? (proportion correct ± 95% CI)
``stat``          one number that is only a number — no chart at all
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import model_validator

from alhazen.config.models import Model

PanelKind = Literal[
    "outcomes",
    "rewards",
    "responses",
    "histogram",
    "scatter",
    "vectors",
    "series",
    "grouped_mean",
    "grouped_rate",
    "performance",
    "stat",
]

# How a ``stat`` panel reduces its column to the single number it shows.
StatAgg = Literal["mean", "median", "sum", "count", "min", "max", "last"]

# How a grouped panel draws its summary. Bars grow from zero, so they suit
# a proportion; a dot with a whisker suits a signed mean, which has no
# meaningful baseline to grow from. Left unset, the kind decides.
GroupedStyle = Literal["dots", "bars"]

# Which group of the dashboard each kind belongs to. Panels are read in
# groups — how the session is going, what the subject did, where it looked,
# how the conditions compare — and the sidebar shows one group at a time, so
# a long dashboard stays something an experimenter can take in.
DEFAULT_SECTIONS: dict[str, str] = {
    "performance": "Session",
    "rewards": "Session",
    "outcomes": "Session",
    "responses": "Behaviour",
    "histogram": "Behaviour",
    "series": "Behaviour",
    "stat": "Behaviour",
    "scatter": "Gaze",
    "vectors": "Gaze",
    "grouped_mean": "Conditions",
    "grouped_rate": "Conditions",
}

# How many of an experiment's condition factors get automatic panels. Two,
# because every factor adds two panels and a dashboard nobody can take in
# at a glance is not monitoring. Declare more explicitly when you want them.
CONDITION_PANEL_LIMIT = 2


class DashboardPanel(Model):
    """One plot. Field names refer to keys in a completed trial record."""

    kind: PanelKind
    title: str
    value: str | None = None
    x: str | None = None
    y: str | None = None
    group: str | None = None
    target_x: str | None = None
    target_y: str | None = None
    # ``vectors`` only: the columns holding the point each trial is measured
    # FROM — the fixation position, usually. Left unset, or naming a column a
    # task does not write, the origin is the screen centre (0, 0) in degrees,
    # which is where this framework's fixation point sits by default; the
    # panel says so rather than assuming it silently.
    origin_x: str | None = None
    origin_y: str | None = None
    # ``scatter`` and ``vectors``: split the points into one coloured series
    # per level of this column, usually an experimental condition. Numeric
    # levels are ordered, so they take one hue light-to-dark; named levels are
    # not, so they take separate hues.
    color_by: str | None = None
    # ``grouped_mean`` and ``grouped_rate``: dots-and-whiskers, or bars.
    style: GroupedStyle | None = None
    # Which sidebar group this panel is filed under. Left unset, it follows
    # from the kind; name your own to file a task's panels together.
    section: str | None = None
    completed_only: bool = False
    rolling_window: int | None = None
    # Unit shown on the value axis (or after a ``stat``'s number). Left unset,
    # it is read off the column name's suffix — ``rt_ms`` is milliseconds,
    # ``endpoint_x_dva`` is degrees of visual angle — which is the naming
    # convention this framework's records already follow. Set it when a column
    # is named in some other way; an axis without a unit is not an axis.
    unit: str | None = None
    # ``stat`` only: which single number the column reduces to.
    agg: StatAgg = "mean"

    @property
    def resolved_section(self) -> str:
        """The sidebar group this panel appears under."""
        return self.section or DEFAULT_SECTIONS[self.kind]

    @model_validator(mode="after")
    def _fields_for_kind(self) -> DashboardPanel:
        if self.kind in {"histogram", "series", "grouped_mean", "stat"} and not self.value:
            raise ValueError(f"{self.kind} panels require value")
        if self.kind in {"grouped_mean", "grouped_rate"} and not self.group:
            raise ValueError(f"{self.kind} panels require group")
        if self.kind in {"scatter", "vectors"} and (not self.x or not self.y):
            raise ValueError(f"{self.kind} panels require x and y")
        if self.rolling_window is not None and self.rolling_window < 1:
            raise ValueError("rolling_window must be >= 1")
        return self


# Ordered by what an experimenter watching a running session looks at first:
# is the subject performing, is it being paid, and how are attempts ending.
# The measurement panels follow.
DEFAULT_PANELS = (
    DashboardPanel(kind="performance", title="Performance"),
    DashboardPanel(kind="rewards", title="Reward earned"),
    DashboardPanel(kind="outcomes", title="Outcomes"),
    DashboardPanel(
        kind="histogram",
        title="Reaction time",
        value="rt_ms",
        completed_only=True,
    ),
    DashboardPanel(kind="responses", title="Responses", value="response_key"),
    DashboardPanel(
        kind="scatter",
        title="Saccade landings",
        x="endpoint_x_dva",
        y="endpoint_y_dva",
        target_x="target_x_dva",
        target_y="target_y_dva",
        completed_only=True,
    ),
    # The same endpoints in the frame the eye actually moved in. The landing
    # panel answers "did it hit the target"; this one answers "how far and
    # which way did it go", with every trial on one origin, so amplitude and
    # direction are readable even when the fixation point moves between
    # trials.
    DashboardPanel(
        kind="vectors",
        title="Landing relative to fixation",
        x="endpoint_x_dva",
        y="endpoint_y_dva",
        origin_x="fixation_x_dva",
        origin_y="fixation_y_dva",
        completed_only=True,
    ),
)


def _condition_panels(field: str) -> tuple[DashboardPanel, ...]:
    """The two panels every experimental factor earns.

    A condition the experiment varies is the thing an experimenter is watching
    for, so it should not take a declaration to see it: does the subject do
    better in one level than another, and does the response land less
    accurately.

    The landing panel groups the *error* rather than the coordinate. A task
    with left and right targets averages its endpoint x to roughly zero, and a
    panel that reported that would be reporting perfect aim.
    """
    label = field.replace("_", " ")
    return (
        DashboardPanel(
            kind="grouped_rate",
            title=f"Accuracy by {label}",
            group=field,
        ),
        DashboardPanel(
            kind="grouped_mean",
            title=f"Landing error by {label}",
            value="endpoint_error_dva",
            group=field,
            style="bars",
            completed_only=True,
        ),
    )


class DashboardSpec(Model):
    """A task's dashboard additions; defaults may be retained or replaced."""

    include_defaults: bool = True
    panels: tuple[DashboardPanel, ...] = ()

    def resolved_panels(self, condition_fields: Sequence[str] = ()) -> tuple[DashboardPanel, ...]:
        """Every panel this session shows, in the order it shows them.

        ``condition_fields`` are the columns the paradigm varies — the session
        runner collects them from the conditions it actually served, so the
        dashboard reflects the experiment that ran rather than one declared in
        advance. They colour the spatial panels and earn panels of their own.
        """
        if not self.include_defaults:
            return self.panels
        fields = tuple(condition_fields)[:CONDITION_PANEL_LIMIT]
        first = fields[0] if fields else None
        defaults = tuple(_coloured_by(panel, first) for panel in DEFAULT_PANELS)
        automatic = tuple(p for field in fields for p in _condition_panels(field))
        return (*defaults, *automatic, *self.panels)


def _coloured_by(panel: DashboardPanel, field: str | None) -> DashboardPanel:
    """Colour a spatial panel by condition, unless the task already said how."""
    if field is None or panel.color_by is not None or panel.kind not in {"scatter", "vectors"}:
        return panel
    return panel.model_copy(update={"color_by": field})
