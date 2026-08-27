"""Loading a scene, and refusing the parts alhazen does not draw.

The refusal is the point. A renderer that quietly skipped a text layer would
produce a stimulus that looks almost right, and "almost right" in a
psychophysics experiment is a result nobody can interpret. So an
out-of-subset feature is an error naming the feature and where in the scene
it sits.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from alhazen.errors import ConfigError
from alhazen.scenes.expr import (
    CONSTANTS,
    FUNCTIONS,
    HOST_IDENTIFIERS,
    REF_IDENTIFIER,
    identifiers,
    param_references,
)
from alhazen.scenes.model import (
    OUT_OF_SUBSET_PRIMITIVES,
    SUPPORTED_PRIMITIVES,
    SUPPORTED_VERSION,
    Layer,
    Scene,
)

log = logging.getLogger(__name__)

# Layer features alhazen does not implement, and what each would change.
UNSUPPORTED_LAYER_FIELDS = {
    "blend": "blend modes",
    "clip": "clipping paths",
}


def load_scene(
    source: str | Path | dict[str, Any],
    declared_params: Iterable[str] | None = None,
) -> Scene:
    """Read and validate a scene: a path, a JSON string, or a parsed dict.

    ``declared_params`` names the extra bare identifiers this scene is
    entitled to read, on top of the function library and the builtin
    variables. Every identifier in every expression is resolved here, so a
    typo surfaces when the file is opened rather than on the frame that
    happens to take the branch containing it.
    """
    data = _as_dict(source)

    version = data.get("version")
    if version != SUPPORTED_VERSION:
        raise ConfigError(
            f"this scene is version {version!r}; alhazen renders version "
            f"{SUPPORTED_VERSION}. Open it in illusion-studio and save it — the studio "
            f"carries the migration between formats, and alhazen deliberately does not "
            f"fork one."
        )

    scene = Scene.model_validate(data)
    known = set(FUNCTIONS) | set(CONSTANTS) | HOST_IDENTIFIERS | set(declared_params or ())
    for index, layer in enumerate(scene.layers):
        _check_layer(layer, f"layers[{index}]", known)
    log.info("scene loaded: %d top-level layers", len(scene.layers))
    return scene


def _as_dict(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    path = Path(source)
    text = str(source)
    # A path that exists is a file; anything else is treated as JSON text, so
    # a caller can pass either without saying which. A very long JSON string
    # can make Path() itself complain on some systems, hence the guard.
    try:
        if path.exists():
            text = path.read_text()
    except OSError:
        pass
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"scene is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"a scene must be a JSON object, got {type(data).__name__}")
    return data


def _check_layer(layer: Layer, path: str, known: set[str]) -> None:
    """Reject anything in this layer alhazen would not draw faithfully."""
    for field, description in UNSUPPORTED_LAYER_FIELDS.items():
        if getattr(layer, field, None) is not None:
            raise ConfigError(
                f"{path} uses {description}, which alhazen does not render. Remove it "
                f"from the scene, or render this scene in the studio instead — alhazen "
                f"will not draw a scene it cannot draw correctly."
            )

    element = layer.element or {}
    kind = element.get("type")
    if kind is None:
        raise ConfigError(f"{path}.element has no 'type'")
    if kind in OUT_OF_SUBSET_PRIMITIVES:
        raise ConfigError(
            f"{path}.element is a {kind!r}, which is outside the subset alhazen renders "
            f"({sorted(SUPPORTED_PRIMITIVES)}). See docs/scenes.md for why each excluded "
            f"primitive is excluded."
        )
    if kind not in SUPPORTED_PRIMITIVES:
        raise ConfigError(
            f"{path}.element has unknown type {kind!r}; alhazen renders "
            f"{sorted(SUPPORTED_PRIMITIVES)}"
        )

    _check_expressions(layer, path, known)

    if kind == "group":
        children = element.get("children") or []
        for index, child in enumerate(children):
            _check_layer(Layer.model_validate(child), f"{path}.children[{index}]", known)


def _check_expressions(layer: Layer, path: str, known: set[str]) -> None:
    """Compile every expression in this layer and resolve its identifiers.

    Two failures this catches at load, both of which previously waited until a
    frame was drawn:

    - ``ref()``. The studio's cross-layer lookup makes evaluation ORDER
      load-bearing, which alhazen deliberately does not implement (spec 7.1).
      Without this the scene loaded cleanly and died with a generic error on
      the first draw, naming neither the feature nor the layer.
    - A typo'd identifier in a branch this render happens not to take. The
      evaluator would only meet it on the frame that took the other side of a
      ternary, which may be minutes into a session.
    """
    for field_path, source in _expressions(layer):
        where = f"{path}.{field_path}"
        try:
            names = identifiers(source)
        except ConfigError as error:
            raise ConfigError(f"{where} does not parse: {error}") from error
        if REF_IDENTIFIER in names:
            raise ConfigError(
                f"{where} uses ref() — outside the subset alhazen renders. A cross-layer "
                f"read makes evaluation order load-bearing, so a scene using one would "
                f"draw differently depending on which layer was evaluated first. Inline "
                f"the value, or render this scene in the studio."
            )
        unknown = sorted(names - known)
        if unknown:
            raise ConfigError(
                f"{where} reads {unknown}, which nothing defines. Known names are the "
                f"function library, the builtin variables {sorted(HOST_IDENTIFIERS)}, and "
                f"the params this scene was loaded with."
            )


def _expressions(layer: Layer) -> Iterator[tuple[str, str]]:
    """Every ``{"expr": ...}`` in a layer, with the path that reaches it."""
    fields: dict[str, Any] = {
        "visible": layer.visible,
        "opacity": layer.opacity,
        "element": layer.element,
        **(layer.model_extra or {}),
    }
    # The group's children are walked as layers of their own, so their
    # expressions are reported under their own paths rather than the group's.
    yield from _walk(fields, "", skip={"children"})


def _walk(node: Any, prefix: str, skip: set[str]) -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        source = node.get("expr")
        if isinstance(source, str):
            yield prefix.strip("."), source
            return
        for name, child in node.items():
            if name in skip:
                continue
            yield from _walk(child, f"{prefix}.{name}", skip)
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _walk(child, f"{prefix}[{index}]", skip)


def scene_param_names(scene: Scene) -> set[str]:
    """Every ``params.<name>`` the scene's expressions read.

    An experiment has to know what a scene wants before it can run one, and
    the scene itself is the honest place to ask.
    """
    names: set[str] = set()
    for index, layer in enumerate(scene.layers):
        _collect_layer_params(layer, f"layers[{index}]", names)
    return names


def _collect_layer_params(layer: Layer, path: str, names: set[str]) -> None:
    for _field_path, source in _expressions(layer):
        names |= param_references(source)
    element = layer.element or {}
    if element.get("type") == "group":
        for index, child in enumerate(element.get("children") or []):
            _collect_layer_params(Layer.model_validate(child), f"{path}.children[{index}]", names)
