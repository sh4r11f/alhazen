"""A downstream experiment adopting alhazen's RF-mapping template.

This is the whole file an experiment needs: subclass the preset for the
area being recorded, give the task its own name (its own data directories,
its own entry in the records), and pin the defaults that describe *your*
site — here, a V4 array whose receptive fields sit in the lower-left
visual field, so the grid is re-centred over it. Everything else — the
trial structure, the probe scheduling, the live map, the dashboard panels,
demo/movie/simulate modes — arrives from the template unchanged.

Per-session tweaks (repetitions, timing) stay in ``task.yaml``; defaults
that describe the preparation belong here, where they are versioned with
the experiment.
"""

from __future__ import annotations

from alhazen.task.templates.rf_mapping import V4RFMapParams, V4RFMapTask


class ArrayV4MapParams(V4RFMapParams):
    # The array's estimated aggregate field: 12x12 degrees around (-5, -4),
    # in the lower-left quadrant. Starting-point numbers — re-centre after
    # the first session's map says where the fields actually are.
    grid_center_x_dva: float = -5.0
    grid_center_y_dva: float = -4.0
    grid_extent_x_dva: float = 12.0
    grid_extent_y_dva: float = 12.0
    grid_cols: int = 10
    grid_rows: int = 10


class ArrayV4MapTask(V4RFMapTask):
    name = "rf-map-v4-array"
    params_model = ArrayV4MapParams
