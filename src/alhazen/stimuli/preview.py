"""Every stimulus an experiment shows, drawn as images: ``alhazen preview``.

Before this, each experiment wrote its own ``preview.py``, and the workspace
ran it with one task's parameter file. So the images depended on which task
and which parameter file were chosen: an experiment with two tasks had its
stimuli split across two scripts and two folders, and the workspace offered
only one of them. Yet the stimuli an experiment shows are the experiment's,
not a parameter file's. A shorter configuration runs fewer of the same
displays; it does not draw different ones.

So the experiment declares its stimuli once, in its pyproject.toml, by naming
the function that draws them::

    [tool.alhazen]
    stimuli = "my_experiment.stimulus_set:stimulus_images"

The function takes the rig's `Screen` (the pixel scale) and returns one
`StimulusImage` per stimulus::

    def stimulus_images(screen: Screen) -> list[StimulusImage]:
        return [StimulusImage("grating-vertical", draw(screen, 90.0), "the target")]

and ``alhazen preview --rig <rig> --out <folder>`` draws them all into the
folder, one PNG each, with an index (``README.md``) listing them. The
workspace's **Preview images** runs the same command, with no task and no
parameter file, because neither changes what it draws.

What this module owns, so that no experiment writes it again:

- finding the declared function (`declared_stimuli`) and importing it from
  the experiment's checkout, installed or not;
- the rules a set of stimuli is held to before anything is written: every
  name a usable file name, no two names that one filesystem would take for
  the same file, no stimulus declared twice under two names, and every image
  a picture a PNG can hold (`StimulusImage` says which);
- writing the folder (`write_preview`), refusing one that holds images the
  declaration no longer has, so the folder always shows exactly the declared
  set.

The PNGs are 8-bit, written with the standard library's zlib (`_png`),
because nothing else in alhazen's core needs an image library: a dependency
for the 20 lines that encode one would make every rig install it.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import struct
import sys
import textwrap
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from alhazen.config.experiment import experiment_stimuli, experiment_title
from alhazen.config.loader import load_rig
from alhazen.config.models import MonitorConfig
from alhazen.config.rigs import RigRef, resolve_rig
from alhazen.display.screen import Screen
from alhazen.errors import ConfigError

# A stimulus's name is its file's name, <name>.png. Held to the characters
# every filesystem alhazen runs on takes in a file name, starting and ending
# with a letter or digit (Windows drops a trailing dot; a leading one hides
# the file elsewhere).
NAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")

# The index's file name and its first line. The line is how a later run
# knows the README in an output folder is its own to replace, rather than a
# page somebody wrote, which it refuses to overwrite.
INDEX_NAME = "README.md"
INDEX_MARKER = "<!-- Written by `alhazen preview`. Run it again rather than editing this file. -->"

# What every PNG file starts with (the PNG specification, section 5.2).
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True, eq=False)
class StimulusImage:
    """One stimulus, as a picture: what a declared function returns, one each.

    ``name`` names the file, ``<name>.png``: letters, digits, ``.``, ``_``
    and ``-``, starting and ending with a letter or digit
    (``kanizsa-aligned-near``).

    ``image`` is the picture at the rig's pixel scale, one array element per
    screen pixel: ``(height, width)`` luminance or ``(height, width, 3)`` RGB,
    as floats in [0, 1] (0 the lowest code value, 1 the highest) or as uint8
    0 to 255. Floats are written as ``round(value * 255)``.

    ``caption`` is one line saying what the stimulus is and what to look for
    in it. It goes into the index beside the image.

    Checked when it is made, so a bad one is refused naming itself, and
    before a single file is written. Not comparable with ``==``: its image is
    an array.
    """

    name: str
    image: np.ndarray
    caption: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not NAME_PATTERN.fullmatch(self.name):
            raise ConfigError(
                f"a stimulus's name becomes its file's name, so it must be letters, digits, "
                f"'.', '_' and '-', starting and ending with a letter or digit; got {self.name!r}"
            )
        if not isinstance(self.caption, str) or "\n" in self.caption or "\r" in self.caption:
            raise ConfigError(
                f"stimulus {self.name!r}: its caption must be one line of text, got "
                f"{self.caption!r}"
            )
        _check_image(self.name, self.image)


def _check_image(name: str, image: Any) -> None:
    """Refuse an image a PNG cannot hold as it stands, saying what to give."""
    if not isinstance(image, np.ndarray):
        raise ConfigError(
            f"stimulus {name!r}: its image must be a numpy array, not {type(image).__name__}"
        )
    colour = image.ndim == 3 and image.shape[2] == 3
    if not (image.ndim == 2 or colour) or image.shape[0] == 0 or image.shape[1] == 0:
        raise ConfigError(
            f"stimulus {name!r}: its image must be (height, width) luminance or "
            f"(height, width, 3) RGB, with at least one pixel; got shape {image.shape}"
        )
    if image.dtype == np.uint8:
        return
    if not np.issubdtype(image.dtype, np.floating):
        raise ConfigError(
            f"stimulus {name!r}: its image is {image.dtype}; give floats in [0, 1] or uint8"
        )
    if not np.all(np.isfinite(image)):
        raise ConfigError(f"stimulus {name!r}: its image holds NaN or infinite values")
    low, high = float(image.min()), float(image.max())
    if low < 0.0 or high > 1.0:
        # Out of range is a mistake upstream (a -1..1 renderer value, say),
        # not something to clip: clipping would write a different picture
        # from the one the experiment shows, without a word.
        raise ConfigError(
            f"stimulus {name!r}: its image runs from {low:g} to {high:g}, but floats must lie "
            "in [0, 1]"
        )


def declared_stimuli(root: Path, screen: Screen) -> list[StimulusImage]:
    """Every stimulus the experiment in folder ``root`` declares, drawn at ``screen``'s scale.

    Imports the function its pyproject.toml names (``[tool.alhazen] stimuli``;
    see `alhazen.config.experiment.experiment_stimuli`), calls it with
    ``screen``, and holds what comes back to the rules in the module
    docstring. The experiment's ``src/`` and folder are put on the import
    path first, as a launched run gets them, so a checkout that was never
    installed works too.

    Raises ConfigError, naming the declaration and its file, when there is no
    usable declaration, its module cannot be imported, it names no function,
    or the function returns something that breaks a rule. An exception the
    experiment's own function raises is left as it is: its traceback is what
    finds the bug.
    """
    root = root.resolve()
    declaration = experiment_stimuli(root)
    pyproject = root / "pyproject.toml"
    if declaration.error is not None:
        raise ConfigError(declaration.error)
    if declaration.target is None:
        raise ConfigError(
            f"{root} declares no stimuli. Name the function that draws them in {pyproject}:\n\n"
            "    [tool.alhazen]\n"
            '    stimuli = "my_experiment.stimulus_set:stimulus_images"\n\n'
            "It takes the rig's Screen and returns one alhazen.stimuli.StimulusImage per "
            "stimulus."
        )
    module_name, function_name = declaration.target.split(":")
    where = f"[tool.alhazen] stimuli = {declaration.target!r} in {pyproject}"
    with _importable_from(root):
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ConfigError(f"{where}: cannot import {module_name}: {exc}") from exc
        function = getattr(module, function_name, None)
        if function is None:
            raise ConfigError(f"{where}: {module_name} has no {function_name}")
        if not callable(function):
            raise ConfigError(
                f"{where}: {module_name}.{function_name} is not a function but "
                f"{type(function).__name__}"
            )
        # Called inside the block: a function that imports more of its own
        # package when it runs needs the path as much as the import did.
        returned = function(screen)
    return _checked_set(returned, where)


@contextmanager
def _importable_from(root: Path) -> Iterator[None]:
    """The experiment's ``src/`` and its folder at the front of the import
    path for the duration, in that order (the workspace's ``_child_env``
    gives a launched run the same two). Each one this added is taken off
    again afterwards; an entry that was already there is left alone."""
    added = [str(p) for p in (root / "src", root) if p.is_dir() and str(p) not in sys.path]
    sys.path[:0] = added
    try:
        yield
    finally:
        for entry in added:
            if entry in sys.path:
                sys.path.remove(entry)


def _checked_set(returned: Any, where: str) -> list[StimulusImage]:
    """What the declared function returned, held to the rules for a set."""
    try:
        images = list(returned)
    except TypeError as exc:
        raise ConfigError(
            f"{where}: the function returned {type(returned).__name__}, not a list of StimulusImage"
        ) from exc
    for position, image in enumerate(images):
        if not isinstance(image, StimulusImage):
            raise ConfigError(
                f"{where}: item {position} is not a StimulusImage but "
                f"{type(image).__name__}; return alhazen.stimuli.StimulusImage objects"
            )
    if not images:
        raise ConfigError(f"{where}: the function returned no stimuli")

    # One file per name, and Windows and macOS take two names that differ
    # only in case for the same file: the second would overwrite the first.
    by_file: dict[str, str] = {}
    for image in images:
        clash = by_file.get(image.name.casefold())
        if clash is not None:
            raise ConfigError(
                f"{where}: two stimuli would be the same file: {clash!r} and {image.name!r}"
            )
        by_file[image.name.casefold()] = image.name

    # A set lists each stimulus once. The same picture under two names is
    # what declaring a parameter file's displays instead of the experiment's
    # looks like (a short configuration's are a subset of the full one's).
    seen: dict[bytes, str] = {}
    for image in images:
        pixels = _pixels(image.image)
        digest = hashlib.sha256(repr(pixels.shape).encode() + pixels.tobytes()).digest()
        if digest in seen:
            raise ConfigError(
                f"{where}: {seen[digest]!r} and {image.name!r} are the same image; declare each "
                "stimulus once"
            )
        seen[digest] = image.name
    return images


def write_preview(root: Path, rig: str | Path, out: Path) -> list[Path]:
    """Draw every stimulus the experiment in ``root`` declares into the folder ``out``.

    At the pixel scale of ``rig`` (a rig's name or file, as ``--rig`` takes
    it, looked up in the experiment's ``configs/`` before alhazen's shared
    rigs): one ``<name>.png`` per stimulus, and the index, ``README.md``,
    listing each with its size and caption. Returns the files written, the
    index last.

    Everything is drawn and checked before anything is written, so a
    declaration that fails leaves ``out`` as it was. ``out`` is refused, and
    left untouched, when it holds a PNG that is not one of the declared
    stimuli (renamed or removed since it was written: delete it, so the
    folder shows exactly the set), or a README.md this command did not write.
    """
    root = root.resolve()
    ref = resolve_rig(rig, root)
    rig_config = load_rig(ref.path)
    screen = Screen.from_monitor(rig_config.monitor)
    images = declared_stimuli(root, screen)
    _check_out(out, images)

    out.mkdir(parents=True, exist_ok=True)
    written = []
    for image in images:
        path = out / f"{image.name}.png"
        path.write_bytes(_png(_pixels(image.image)))
        written.append(path)
    index = out / INDEX_NAME
    index.write_text(_index(root, ref, rig_config.monitor, screen, out, images), encoding="utf-8")
    written.append(index)
    return written


def _check_out(out: Path, images: list[StimulusImage]) -> None:
    """Refuse a folder writing into would leave misleading, or would destroy."""
    if out.exists() and not out.is_dir():
        raise ConfigError(f"--out {out} is a file, not a folder")
    if not out.is_dir():
        return
    declared = {image.name.casefold() for image in images}
    stale = sorted(
        path.name
        for path in out.iterdir()
        if path.suffix.lower() == ".png" and path.stem.casefold() not in declared
    )
    if stale:
        raise ConfigError(
            f"{out} holds images that are not among the declared stimuli: {', '.join(stale)}. "
            "Delete them, or choose an empty folder, so the folder shows exactly the stimuli "
            "the experiment declares. Nothing was written"
        )
    index = out / INDEX_NAME
    if index.is_file():
        with index.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline().rstrip("\r\n")
        if first != INDEX_MARKER:
            raise ConfigError(
                f"{index} was not written by alhazen preview, and this command writes its index "
                "there. Move it, or choose another folder. Nothing was written"
            )


def _pixels(image: np.ndarray) -> np.ndarray:
    """An image as the 8-bit code values a PNG holds, rows contiguous: uint8
    as given; floats in [0, 1] times 255, rounded to the nearest (half to
    even, as numpy rounds)."""
    if image.dtype == np.uint8:
        return np.ascontiguousarray(image)
    return np.ascontiguousarray(np.round(image * 255.0).astype(np.uint8))


def _png(pixels: np.ndarray) -> bytes:
    """An 8-bit greyscale or RGB PNG holding ``pixels`` exactly.

    The smallest file the specification allows: the signature, a header
    (IHDR), the image data (IDAT) and the end (IEND). Each row of the data is
    prefixed with its filter type, 0 (none), which every decoder reads, and
    the rows together are compressed with zlib.
    """
    height, width = pixels.shape[:2]
    colour_type = 0 if pixels.ndim == 2 else 2  # greyscale, or truecolour RGB
    rows = pixels.reshape(height, -1)
    filtered = np.concatenate([np.zeros((height, 1), dtype=np.uint8), rows], axis=1)
    header = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(filtered.tobytes(), 9))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, data: bytes) -> bytes:
    """One PNG chunk: its length, its type, its data, and a CRC of type and data."""
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _index(
    root: Path,
    ref: RigRef,
    monitor: MonitorConfig,
    screen: Screen,
    out: Path,
    images: list[StimulusImage],
) -> str:
    """The README written beside the images: what they are, at what scale,
    how to make them again, and each one with its caption."""
    title = experiment_title(root).title
    target = experiment_stimuli(root).target
    # The command to write them again, as it would be typed in the
    # experiment's folder. An --out outside that folder (a workspace run's
    # media folder) is not spelled out: it is no use to anyone reading a copy
    # of this file elsewhere, and it would put this machine's paths in it.
    try:
        shown_out = out.resolve().relative_to(root).as_posix()
    except ValueError:
        shown_out = "<this folder>"
    # One paragraph, wrapped here rather than by hand: the function's name
    # and the rig's are as long as the experiment makes them.
    about = textwrap.fill(
        f"The {len(images)} stimuli this experiment declares (`{target}`, named in its "
        "pyproject.toml), each drawn one image pixel per screen pixel at the scale of the "
        f"rig `{ref.spec}`: {screen.px_per_deg:.2f} px per degree, on a "
        f"{monitor.width_px} x {monitor.height_px} px panel. To draw them again, in the "
        "experiment's folder:",
        width=80,
        break_long_words=False,
        break_on_hyphens=False,
    )
    lines = [
        INDEX_MARKER,
        "",
        f"# {title}: every stimulus",
        "",
        about,
        "",
        f"    alhazen preview --rig {ref.spec} --out {shown_out}",
    ]
    for image in images:
        height, width = image.image.shape[:2]
        lines += ["", f"## {image.name}", ""]
        if image.caption:
            lines += [image.caption, ""]
        lines += [f"{width} x {height} px", "", f"![{image.name}]({image.name}.png)"]
    return "\n".join(lines) + "\n"
