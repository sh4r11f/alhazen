"""The calibration pictures alhazen ships: their names, files and manifest.

The pictures themselves live beside the shared rigs, as package data
(``src/alhazen/calibration_images/``), so an installed wheel calibrates with
them on a rig with no network. This module is the one place that knows where
they are and which names exist, so a rig config can be checked against the
names when it loads (``EyeTrackerConfig``) without decoding a single image;
decoding is the display side's job, done once before a calibration
(``alhazen.devices.eyetracker.calibration_targets``).

``manifest.json`` is the record of what the files are: each one's SHA-256,
size and pixel dimensions, and where it came from. :func:`verified_bytes` holds
every read to it, so a file replaced or truncated on disk is refused by name
rather than shown to a subject.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from alhazen.errors import ConfigError

IMAGE_DIR = Path(__file__).resolve().parent.parent / "calibration_images"
MANIFEST_FILE = "manifest.json"
# A picture's name: its file name without ".png". Lowercase letters, digits
# and underscores only, so a name can never be a path ("../x", "a/b") — the
# workspace serves these files by name, and only by a name in the manifest.
NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class CalibrationImage:
    """One picture as the manifest records it."""

    name: str
    file: str
    sha256: str
    width_px: int
    height_px: int


@cache
def manifest() -> dict[str, Any]:
    """The manifest, read once. A missing or unreadable one is an incomplete
    installation (a wheel built without its package data), said so."""
    path = IMAGE_DIR / MANIFEST_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(
            f"alhazen's calibration pictures are missing: {path} does not exist. The "
            "installation is incomplete; reinstall alhazen-vision."
        ) from e
    except ValueError as e:
        raise ConfigError(
            f"alhazen's calibration picture manifest {path} is unreadable: {e}"
        ) from e
    if not isinstance(data, dict) or not isinstance(data.get("images"), list):
        raise ConfigError(f"alhazen's calibration picture manifest {path} has no image list")
    return data


@cache
def catalog() -> dict[str, CalibrationImage]:
    """Every shipped picture, by name, in the manifest's order."""
    images: dict[str, CalibrationImage] = {}
    for entry in manifest()["images"]:
        try:
            image = CalibrationImage(
                name=str(entry["name"]),
                file=str(entry["file"]),
                sha256=str(entry["sha256"]),
                width_px=int(entry["width_px"]),
                height_px=int(entry["height_px"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(
                f"calibration picture manifest entry {entry!r} is malformed: {e}"
            ) from e
        if not NAME_PATTERN.fullmatch(image.name) or image.file != f"{image.name}.png":
            raise ConfigError(f"calibration picture manifest names an unsafe file: {image.file!r}")
        images[image.name] = image
    return images


def image_names() -> tuple[str, ...]:
    """The names a config may choose from, in the manifest's order."""
    return tuple(catalog())


def unknown_names(names: list[str] | tuple[str, ...]) -> list[str]:
    """The names in ``names`` that are not shipped pictures."""
    known = catalog()
    return [name for name in names if name not in known]


def image_path(name: str) -> Path:
    """The file of a shipped picture. Only a manifest name has one: anything
    else, a path included, is a ConfigError, never a file lookup."""
    image = catalog().get(name)
    if image is None:
        raise ConfigError(
            f"{name!r} is not one of alhazen's calibration pictures; choose from "
            f"{', '.join(image_names())}"
        )
    return IMAGE_DIR / image.file


def verified_bytes(name: str) -> bytes:
    """A picture's bytes, after checking them against the manifest.

    Raises ConfigError when the file is missing or its SHA-256 differs: a
    picture that is not the recorded one is not shown, and the message names
    the file so it can be reinstalled.
    """
    path = image_path(name)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ConfigError(
            f"calibration picture {name!r} cannot be read from {path}: {e}. Reinstall "
            "alhazen-vision."
        ) from e
    digest = hashlib.sha256(data).hexdigest()
    if digest != catalog()[name].sha256:
        raise ConfigError(
            f"calibration picture {name!r} ({path}) does not match alhazen's manifest "
            f"(SHA-256 {digest}, expected {catalog()[name].sha256}); it has been changed "
            "or damaged. Reinstall alhazen-vision."
        )
    return data


__all__ = [
    "IMAGE_DIR",
    "CalibrationImage",
    "catalog",
    "image_names",
    "image_path",
    "manifest",
    "unknown_names",
    "verified_bytes",
]
