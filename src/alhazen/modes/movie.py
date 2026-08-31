"""Movie mode: write the stimulus to files, for a demo you can send.

A moving stimulus is the one part of an experiment a figure in a paper cannot
carry, and `--mode demo` only helps the person in the room. Both of alhazen's
experiments needed to show their conditions to people who were not at the rig
— collaborators, a lab meeting, reviewers — and the first one grew its own
four hundred lines of encoder plumbing to do it. The encoder, the pixel
conversion, the tiling and the labelling are the same for every experiment;
what differs is the pixels, which is the experiment's own business and the
only part it should have to write.

**What a movie is, exactly.** Every frame is composited by the experiment's
own code — the same geometry a trial would draw — on the screen of the rig
named by ``--rig``, at that rig's own resolution, pixels-per-degree and
refresh rate. Cut movies against the lab rig's config, not a laptop's: the
movie previews the rig, and the rig is where the sizes are true.

**What it is not.** It does not open a window and does not capture one, so it
can say nothing about frame timing, tearing, or what a monitor actually
emitted — `--mode measure` answers for the machine, and the frame-QA log
answers for a session. And it is not the renderer's own output: judging
whether the stimulus *looks right* is `--mode demo`'s job, on a real display.

The experiment's side of the contract is one method::

    class MyTask(Task):
        def movie_clips(self, setup: MovieSetup) -> list[MovieClip]:
            ...

Each :class:`MovieClip` names one file and supplies its frames; everything
after the frames — encoding, scaling, the contact sheet — is this module.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from alhazen.display.screen import Screen
from alhazen.errors import ConfigError

# What a clip may be called. It becomes a filename stem, so path separators
# and leading dots are refused rather than written into the filesystem.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The sheet's furniture colours, as 8-bit grey. Chosen to read on anything:
# the strip is drawn as its own band rather than over the frames, so it never
# has to compete with a stimulus for contrast.
SHEET_BACKGROUND = 0
SHEET_INK = 220

# The file every clip lands in when --sheet is given without a path.
DEFAULT_SHEET_NAME = "all-clips.mp4"


@dataclass(frozen=True)
class MovieSetup:
    """What an experiment gets to build its clips from.

    The same quantities a trial would get — the screen geometry and refresh
    rate come from the rig config the command named — minus the display,
    because a movie never opens one.
    """

    screen: Screen
    hz: float
    params: Any
    rng: np.random.Generator


@dataclass(frozen=True)
class MovieClip:
    """One file to write: a name and a stream of frames.

    ``frames`` is a zero-argument callable returning a fresh, finite stream —
    a generator function fits naturally — rather than the stream itself, so a
    clip can be recorded more than once (a file of its own and a panel of the
    sheet) without the second read finding it exhausted.

    Each frame is a numpy array, either ``(height, width)`` luminance or
    ``(height, width, 3)`` RGB, as float in 0..1 or as uint8 — the same
    conventions the headless scene renderer uses. One frame per screen flip:
    the stream's length in frames divided by the rig's refresh rate IS the
    clip's duration, which is what keeps the movie's clock the session's.
    """

    name: str
    frames: Callable[[], Iterable[np.ndarray]]
    # What the contact sheet prints above this clip's panel. The name is a
    # filename and stays terse; the label may say more.
    label: str | None = None

    @property
    def caption(self) -> str:
        return self.label if self.label is not None else self.name


# ----------------------------------------------------------------------
# Pixels: what a frame must be, and how it becomes a file's
# ----------------------------------------------------------------------


def to_uint8(frame: np.ndarray, clip_name: str) -> np.ndarray:
    """One frame in the encoder's terms: 8-bit, grey or RGB.

    Anything else is refused by name rather than coerced. A float frame with
    values outside 0..1 means the experiment's compositing is broken — clipped
    quietly, that bug ships in a movie that looks merely "a bit off", which is
    the worst possible place to discover a rendering error.
    """
    array = np.asarray(frame)
    if array.ndim not in (2, 3) or (array.ndim == 3 and array.shape[-1] != 3):
        raise ConfigError(
            f"clip {clip_name!r} yielded a frame of shape {array.shape}; a frame is "
            f"(height, width) luminance or (height, width, 3) RGB"
        )
    if array.dtype == np.uint8:
        return array
    if not np.issubdtype(array.dtype, np.floating):
        raise ConfigError(
            f"clip {clip_name!r} yielded a {array.dtype} frame; frames are float in 0..1 or uint8"
        )
    if not np.all(np.isfinite(array)):
        raise ConfigError(f"clip {clip_name!r} yielded a frame with NaN or infinity in it")
    if array.min() < 0.0 or array.max() > 1.0:
        raise ConfigError(
            f"clip {clip_name!r} yielded float values outside 0..1 "
            f"(min {array.min():.4g}, max {array.max():.4g}); scale the frame before "
            f"yielding it — clipping here would hide a compositing bug in plain sight"
        )
    return (array * 255.0).round().astype(np.uint8)


def scale_frame(frame: np.ndarray, scale: float) -> np.ndarray:
    """Shrink one 8-bit frame by ``scale``, by area averaging.

    Shrink only: a movie is for looking at, and upsampling invents pixels the
    display never had. The area filter (PIL's BOX) is exact block averaging
    for an integer factor and close enough for a fraction.
    """
    if scale == 1.0:
        return frame
    if not 0.0 < scale <= 1.0:
        raise ConfigError(f"--scale must be in (0, 1], got {scale:g} — movies only shrink")
    from PIL import Image

    height = max(1, round(frame.shape[0] * scale))
    width = max(1, round(frame.shape[1] * scale))
    return np.asarray(Image.fromarray(frame).resize((width, height), Image.Resampling.BOX))


def even_dims(frame: np.ndarray) -> np.ndarray:
    """Grow the frame to even width and height, by one row or column of edge.

    h264 encodes in 2x2 blocks and refuses an odd dimension outright ("width
    not divisible by 2"), writing nothing. Which dimensions come out odd
    depends on the frames, the grid and the scale, so it is not a case to
    avoid by choosing good numbers — it is one to handle. Edge replication
    rather than a constant, so the added line is whatever the frame already
    had at that edge.
    """
    pad_r, pad_c = frame.shape[0] % 2, frame.shape[1] % 2
    if not (pad_r or pad_c):
        return frame
    pad: list[tuple[int, int]] = [(0, pad_r), (0, pad_c)]
    if frame.ndim == 3:
        pad.append((0, 0))
    return np.pad(frame, pad, mode="edge")


def _writer(path: Path, hz: float) -> Any:
    """The encoder, or a message naming the extra that installs it.

    imageio-ffmpeg ships an ffmpeg binary, which is a lot to put on a rig
    that will never write a movie — so it is an extra, and the ImportError is
    translated into what to type rather than left as "No module named
    imageio", which says neither.
    """
    try:
        import imageio.v2 as imageio
    except ImportError as exc:
        raise ConfigError(
            "movie mode needs an encoder, which is an optional extra because it "
            "ships an ffmpeg binary. Install it with:\n"
            '    pip install "alhazen-vision[movie]"'
        ) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    # macro_block_size=None: the frames are already padded to even dimensions
    # by even_dims, and ffmpeg's own resize-to-16 would silently rescale them.
    return imageio.get_writer(path, fps=hz, macro_block_size=None)


# ----------------------------------------------------------------------
# Recording: one clip per file, or every clip on one sheet
# ----------------------------------------------------------------------


def record_clip(clip: MovieClip, hz: float, path: Path, scale: float = 1.0) -> int:
    """Write one clip to ``path``; returns how many frames were written.

    Streamed rather than collected: a few seconds of trial at 120 Hz on a
    1920x1080 rig is hundreds of full-screen frames, and holding them all in
    memory scales with the rig, not with this module.
    """
    written = 0
    writer = _writer(path, hz)
    try:
        for frame in clip.frames():
            writer.append_data(even_dims(scale_frame(to_uint8(frame, clip.name), scale)))
            written += 1
    finally:
        writer.close()
    if written == 0:
        # The zero-frame file is deleted so a directory listing cannot show a
        # movie that plays nothing — that reads as an encoder problem and
        # sends someone debugging ffmpeg instead of their own generator.
        path.unlink(missing_ok=True)
        raise ConfigError(f"clip {clip.name!r} yielded no frames — nothing to record")
    return written


def record_sheet(
    clips: list[MovieClip],
    hz: float,
    path: Path,
    columns: int | None = None,
    scale: float = 1.0,
) -> int:
    """Every clip at once, tiled on one canvas with its caption above it.

    A sheet exists to be compared across, so the panels have to be
    commensurable: every clip must yield frames of the same shape, and every
    panel gets the same scale. A sheet that fitted each panel to its own
    content would resize away exactly the differences a condition grid is
    built to show.

    The clips run on one clock — each sheet frame takes the next frame of
    every stream — and a clip that ends early holds its last frame until the
    longest one finishes. Holding rather than blanking, because a trial's end
    state is part of what it shows, and a panel that blinks to black reads as
    a dropped stream rather than a shorter trial.

    Returns how many sheet frames were written.
    """
    from PIL import Image, ImageDraw

    if not clips:
        raise ConfigError("a sheet needs at least one clip")
    if columns is None:
        # Near-square by default: the layout is a viewing convenience, and an
        # experiment that wants rows to mean something passes --columns.
        columns = math.ceil(math.sqrt(len(clips)))
    if columns < 1:
        raise ConfigError(f"--columns must be at least 1, got {columns}")
    rows = math.ceil(len(clips) / columns)

    streams: list[Iterator[np.ndarray]] = [iter(clip.frames()) for clip in clips]
    # The first frame of each stream, fetched before any layout: the panel
    # size comes from the frames, and a clip with none is refused by name
    # before a file exists.
    current: list[np.ndarray] = []
    for clip, stream in zip(clips, streams, strict=True):
        first = next(stream, None)
        if first is None:
            raise ConfigError(f"clip {clip.name!r} yielded no frames — nothing to put on the sheet")
        current.append(scale_frame(to_uint8(first, clip.name), scale))

    shape = current[0].shape[:2]
    for clip, frame in zip(clips, current, strict=True):
        if frame.shape[:2] != shape:
            raise ConfigError(
                f"sheet panels must all be the same size, but clip {clips[0].name!r} "
                f"is {shape[1]}x{shape[0]} and clip {clip.name!r} is "
                f"{frame.shape[1]}x{frame.shape[0]} (width x height, after --scale). "
                f"Yield same-shaped frames, or record per-clip files instead."
            )
    # One grey panel promotes the whole sheet to RGB rather than being
    # broadcast wrongly into it; an all-grey sheet stays grey, which encodes
    # smaller.
    rgb = any(frame.ndim == 3 for frame in current)

    panel_h, panel_w = shape
    font, label_h = _fit_font(max((clip.caption for clip in clips), key=len), panel_w)
    sheet_w = panel_w * columns
    sheet_h = (panel_h + label_h) * rows

    alive = [True] * len(clips)
    written = 0
    writer = _writer(path, hz)
    try:
        while True:
            canvas = np.full(
                (sheet_h, sheet_w, 3) if rgb else (sheet_h, sheet_w),
                SHEET_BACKGROUND,
                dtype=np.uint8,
            )
            for index, frame in enumerate(current):
                top = (index // columns) * (panel_h + label_h) + label_h
                left = (index % columns) * panel_w
                canvas[top : top + panel_h, left : left + panel_w] = (
                    _promote(frame) if rgb else frame
                )
            image = Image.fromarray(canvas)
            draw = ImageDraw.Draw(image)
            for index, clip in enumerate(clips):
                # Centred over its panel, anchored to the strip's top, so a
                # caption never overlaps the frame below it.
                centre = ((index % columns) + 0.5) * panel_w
                top = (index // columns) * (panel_h + label_h) + label_h * 0.15
                draw.text((centre, top), clip.caption, fill=SHEET_INK, font=font, anchor="ma")
            writer.append_data(even_dims(np.asarray(image)))
            written += 1

            # Advance every stream together; the sheet ends when the last
            # one does. A finished panel keeps its final frame (see above).
            for index, stream in enumerate(streams):
                if not alive[index]:
                    continue
                step = next(stream, None)
                if step is None:
                    alive[index] = False
                else:
                    current[index] = scale_frame(to_uint8(step, clips[index].name), scale)
                    if current[index].shape[:2] != shape:
                        raise ConfigError(
                            f"clip {clips[index].name!r} changed frame size mid-stream "
                            f"(was {shape[1]}x{shape[0]}, now "
                            f"{current[index].shape[1]}x{current[index].shape[0]})"
                        )
            if not any(alive):
                break
    finally:
        writer.close()
    return written


def _promote(frame: np.ndarray) -> np.ndarray:
    """A grey frame as RGB, so it can sit on an RGB sheet."""
    return frame if frame.ndim == 3 else np.repeat(frame[:, :, None], 3, axis=2)


def _fit_font(longest: str, panel_width: int) -> tuple[Any, int]:
    """The largest default font whose longest caption still fits its panel.

    Fitted by measuring rather than guessed from the panel height — a caption
    that runs into the next column makes two conditions read as one. Returns
    the font and the height of the label strip it needs.
    """
    from PIL import ImageFont

    room = max(8, panel_width - 12)
    for size in range(22, 7, -1):
        font = ImageFont.load_default(size=size)
        box = font.getbbox(longest)
        if box[2] - box[0] <= room:
            break
    # The strip is taller than the glyphs so descenders clear the frame below.
    return font, round((box[3] - box[1]) * 1.9)


# ----------------------------------------------------------------------
# The mode: what `--mode movie` runs
# ----------------------------------------------------------------------


def run_movie(
    setup_clips: Callable[[MovieSetup], list[MovieClip]],
    *,
    rig: Any,
    params: Any,
    out: str | Path = "movies",
    clip_names: tuple[str, ...] = (),
    sheet: str | Path | None = None,
    columns: int | None = None,
    scale: float = 1.0,
    seed: int = 0,
    echo: Callable[[str], None] = print,
) -> int:
    """Build the task's clips against this rig and write them out.

    With ``sheet`` set, one tiled file; otherwise one file per clip under
    ``out``. ``clip_names`` narrows either to the clips it names, and a name
    that matches nothing is refused with the list of what exists — a filter
    that silently matched nothing would look like a working command that
    produced no output.
    """
    screen = Screen.from_monitor(rig.monitor)
    hz = float(rig.monitor.refresh_rate_hz)
    setup = MovieSetup(screen=screen, hz=hz, params=params, rng=np.random.default_rng(seed))

    clips = setup_clips(setup)
    if not clips:
        raise ConfigError("movie_clips returned no clips — nothing to record")
    names = [clip.name for clip in clips]
    for name in names:
        if not NAME_PATTERN.match(name):
            raise ConfigError(
                f"clip name {name!r} cannot be a filename: use letters, digits, "
                f"dots, hyphens and underscores, starting with a letter or digit"
            )
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(
            f"clip names are used twice: {duplicates}. One clip, one file — a "
            f"duplicate would silently overwrite the first recording."
        )
    if clip_names:
        unknown = [name for name in clip_names if name not in names]
        if unknown:
            raise ConfigError(
                f"no clip named {', '.join(repr(name) for name in unknown)} — this "
                f"task records {', '.join(names)}"
            )
        clips = [clip for clip in clips if clip.name in clip_names]

    if sheet is not None:
        target = Path(sheet)
        frames = record_sheet(clips, hz, target, columns=columns, scale=scale)
        echo(f"{len(clips)} clip(s) on one sheet, {frames / hz:.2f} s at {hz:g} Hz")
        echo(f"  {target}")
        return 0

    directory = Path(out)
    echo(f"{len(clips)} clip(s) at {hz:g} Hz, into {directory}")
    for clip in clips:
        target = directory / f"{clip.name}.mp4"
        frames = record_clip(clip, hz, target, scale=scale)
        echo(f"  {target}  ({frames / hz:.2f} s)")
    return 0
