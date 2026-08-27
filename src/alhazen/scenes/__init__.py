"""Scenes: stimuli designed in illusion-studio, run unchanged in experiments.

A scene is JSON — shapes, gratings, dot fields, and expressions that animate
them. The studio is where they are designed; this renders a documented subset
of them inside a trial, deterministically: same scene, same params, same time,
same pixels.

Three pieces: an expression evaluator that is a parser rather than ``eval``, a
loader that refuses anything outside the subset by name, and a renderer whose
primary path is headless — so what an experiment shows is exactly what a test
can inspect on a machine with nothing installed.
"""

from alhazen.scenes.expr import EvalContext, compile_expr, evaluate_expr
from alhazen.scenes.loader import load_scene, scene_param_names
from alhazen.scenes.model import SUPPORTED_PRIMITIVES, SUPPORTED_VERSION, Scene
from alhazen.scenes.render import RenderContext, SceneStimulus, headless_render
from alhazen.scenes.rng import mulberry32

__all__ = [
    "SUPPORTED_PRIMITIVES",
    "SUPPORTED_VERSION",
    "EvalContext",
    "RenderContext",
    "Scene",
    "SceneStimulus",
    "compile_expr",
    "evaluate_expr",
    "headless_render",
    "load_scene",
    "scene_param_names",
    "mulberry32",
]
