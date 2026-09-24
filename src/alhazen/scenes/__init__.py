"""Scenes: stimuli designed in illusion-studio, run unchanged in experiments.

A scene is JSON — shapes, gratings, dot fields, and expressions that animate
them. The studio is where they are designed; this renders a documented subset
of them inside a trial, deterministically: same scene, same params, same time
and dt, same pixels. Inside a trial, time follows the measured flips, so a run
that drops a frame shows a different sequence of frames (``SceneStimulus``).

Three pieces: an expression evaluator that is a parser rather than ``eval``, a
loader that refuses anything outside the subset by name, and a renderer whose
primary path is headless — so what an experiment shows is exactly what a test
can inspect on a machine with nothing installed.
"""

from alhazen.scenes.expr import EvalContext as EvalContext
from alhazen.scenes.expr import compile_expr as compile_expr
from alhazen.scenes.expr import evaluate_expr as evaluate_expr
from alhazen.scenes.loader import load_scene, scene_param_names
from alhazen.scenes.model import SUPPORTED_PRIMITIVES as SUPPORTED_PRIMITIVES
from alhazen.scenes.model import SUPPORTED_VERSION as SUPPORTED_VERSION
from alhazen.scenes.model import Scene
from alhazen.scenes.render import RenderContext as RenderContext
from alhazen.scenes.render import SceneStimulus, headless_render
from alhazen.scenes.rng import mulberry32 as mulberry32

# `__all__` holds only the names docs/reference.md lists as public (a test in
# tests/unit/test_docs_snippets.py holds it to that). The `X as X` imports
# above are internal: they stay importable from here, for code that already
# imports them this way, but are not exported.
__all__ = [
    "Scene",
    "SceneStimulus",
    "headless_render",
    "load_scene",
    "scene_param_names",
]
