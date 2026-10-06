"""``alhazen preview``: an experiment's declared stimuli, drawn as PNG files.

Every test builds a small experiment in a temporary folder (a pyproject.toml,
a package under src/ that is never installed, a rig file) and runs the real
import, the real checks and the real writer against it. Each experiment's
package gets its own name, because Python keeps an imported module for the
rest of the process and two tests sharing a name would see each other's.
"""

from __future__ import annotations

import io
import itertools
import struct
import sys
import textwrap
import zlib
from pathlib import Path

import numpy as np
import pytest

from alhazen.cli.main import main
from alhazen.display.screen import Screen
from alhazen.errors import ConfigError
from alhazen.stimuli import StimulusImage
from alhazen.stimuli.preview import (
    INDEX_MARKER,
    PNG_SIGNATURE,
    _pixels,
    _png,
    declared_stimuli,
    write_preview,
)

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"
SCREEN = Screen(width_px=1920, height_px=1080, px_per_deg=40.0)

# A fresh package name for each experiment built in this module.
_names = itertools.count()

# The stimulus module most tests use: two stimuli, one grey and one colour,
# whose sizes come from the screen it is handed, so a test can see that the
# rig's scale reached it.
TWO_STIMULI = """
import numpy as np
from alhazen.stimuli import StimulusImage

def stimulus_images(screen):
    size = int(round(screen.px_per_deg))
    grey = np.full((size, 2 * size), 0.5)
    red = np.zeros((size, size, 3))
    red[..., 0] = 1.0
    return [
        StimulusImage("grey-field", grey, "a mid-grey field, twice as wide as it is tall"),
        StimulusImage("red-square", red, "a red square, one degree on a side"),
    ]
"""


def make_experiment(
    folder: Path, source: str = TWO_STIMULI, declaration: str | None = None
) -> tuple[Path, str]:
    """An experiment in ``folder`` whose package's ``stimulus_set`` module is
    ``source``. Its pyproject declares ``<package>.stimulus_set:stimulus_images``,
    or the ``declaration`` line given (``{package}`` is filled in). Returns
    the folder and the package's name."""
    package = f"preview_experiment_{next(_names)}"
    if declaration is None:
        declaration = 'stimuli = "{package}.stimulus_set:stimulus_images"'
    line = declaration.format(package=package)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "0.1.0"\n\n'
        f'[tool.alhazen]\ntitle = "Demo experiment"\n{line}\n',
        encoding="utf-8",
    )
    (folder / "src" / package).mkdir(parents=True)
    (folder / "src" / package / "__init__.py").write_text("", encoding="utf-8")
    (folder / "src" / package / "stimulus_set.py").write_text(
        textwrap.dedent(source), encoding="utf-8"
    )
    (folder / "configs").mkdir()
    (folder / "configs" / "rig-sim.yaml").write_bytes(RIG.read_bytes())
    return folder, package


def read_png(data: bytes) -> np.ndarray:
    """Decode an 8-bit, unfiltered, non-interlaced PNG, checking every rule
    on the way: written here from the specification, independently of the
    encoder under test."""
    assert data[:8] == PNG_SIGNATURE
    position, chunks = 8, []
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length : position + 12 + length])
        assert crc == zlib.crc32(kind + body), f"bad CRC on {kind!r}"
        chunks.append((kind, body))
        position += 12 + length
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", chunks[0][1]
    )
    assert (depth, compression, filtering, interlace) == (8, 0, 0, 0)
    channels = {0: 1, 2: 3}[colour]
    raw = np.frombuffer(zlib.decompress(chunks[1][1]), dtype=np.uint8)
    rows = raw.reshape(height, 1 + width * channels)
    assert np.all(rows[:, 0] == 0), "every row must use filter type 0"
    pixels = rows[:, 1:]
    return pixels.reshape(height, width) if channels == 1 else pixels.reshape(height, width, 3)


# ----------------------------------------------------------------------
# One stimulus: what may be declared
# ----------------------------------------------------------------------


class TestStimulusImage:
    @pytest.mark.parametrize(
        "image",
        [
            np.zeros((3, 4)),
            np.ones((3, 4, 3), dtype=np.float32),
            np.full((3, 4), 255, dtype=np.uint8),
            np.zeros((3, 4, 3), dtype=np.uint8),
        ],
        ids=["grey floats", "colour floats", "grey uint8", "colour uint8"],
    )
    def test_luminance_and_colour_as_floats_or_bytes_are_accepted(self, image):
        assert StimulusImage("ok", image).image is image

    @pytest.mark.parametrize(
        "name", ["", "two words", "-leading", "trailing-", "trailing.", "a/b", "a\\b", "é"]
    )
    def test_a_name_that_cannot_be_a_file_name_is_refused(self, name):
        with pytest.raises(ConfigError, match="file's name"):
            StimulusImage(name, np.zeros((2, 2)))

    def test_a_caption_of_more_than_one_line_is_refused(self):
        with pytest.raises(ConfigError, match="one line"):
            StimulusImage("x", np.zeros((2, 2)), "first\nsecond")

    @pytest.mark.parametrize(
        ("image", "says"),
        [
            ([[0.0, 1.0]], "numpy array"),
            (np.zeros(4), "shape"),
            (np.zeros((2, 2, 4)), "shape"),
            (np.zeros((2, 2, 1)), "shape"),
            (np.zeros((0, 3)), "at least one pixel"),
            (np.zeros((2, 2), dtype=np.int64), "int64"),
            (np.array([[0.0, np.nan]]), "NaN"),
            (np.array([[-0.1, 0.5]]), "from -0.1 to 0.5"),
            (np.array([[0.5, 1.2]]), "from 0.5 to 1.2"),
        ],
        ids=["list", "1-D", "RGBA", "one channel", "empty", "integers", "NaN", "below", "above"],
    )
    def test_an_image_a_png_cannot_hold_as_it_stands_is_refused_by_name(self, image, says):
        with pytest.raises(ConfigError, match="'bad'") as error:
            StimulusImage("bad", image)
        assert says in str(error.value)


# ----------------------------------------------------------------------
# The PNG encoder
# ----------------------------------------------------------------------


class TestPng:
    @pytest.mark.parametrize("shape", [(5, 7), (5, 7, 3), (1, 1), (1, 1, 3)])
    def test_the_pixels_come_back_exactly(self, shape):
        pixels = np.random.default_rng(0).integers(0, 256, size=shape, dtype=np.uint8)
        assert np.array_equal(read_png(_png(pixels)), pixels)

    def test_an_independent_decoder_reads_the_same_pixels(self):
        # Pillow comes with the dev extra (alhazen-vision[movie]), so this
        # runs wherever the suite does rather than skipping itself.
        from PIL import Image

        for shape in [(6, 9), (6, 9, 3)]:
            pixels = np.random.default_rng(1).integers(0, 256, size=shape, dtype=np.uint8)
            decoded = np.asarray(Image.open(io.BytesIO(_png(pixels))))
            assert np.array_equal(decoded, pixels)

    def test_floats_become_code_values_as_the_experiments_rounded_them(self):
        # round(value * 255), half to even: what `(x * 255).round()` gave
        # every experiment that wrote its own PNGs before this.
        floats = np.array([[0.0, 1.0, 0.5, 0.25, 127.5 / 255]])
        assert _pixels(floats).tolist() == [[0, 255, 128, 64, 128]]
        assert _pixels(floats).dtype == np.uint8


# ----------------------------------------------------------------------
# Finding and running the declared function
# ----------------------------------------------------------------------


class TestDeclaredStimuli:
    def test_the_declared_function_draws_at_the_screens_scale(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp")
        images = declared_stimuli(root, SCREEN)
        assert [image.name for image in images] == ["grey-field", "red-square"]
        assert images[0].image.shape == (40, 80)  # 40 px per degree reached it
        assert images[1].caption == "a red square, one degree on a side"

    def test_the_import_path_is_left_as_it_was(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp")
        before = list(sys.path)
        declared_stimuli(root, SCREEN)
        assert sys.path == before

    def test_an_experiment_that_declares_nothing_is_told_what_to_write(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp", declaration="")
        with pytest.raises(ConfigError, match="declares no stimuli") as error:
            declared_stimuli(root, SCREEN)
        assert "[tool.alhazen]" in str(error.value)
        assert str(root / "pyproject.toml") in str(error.value)

    def test_a_malformed_declaration_is_refused_naming_the_file(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp", declaration='stimuli = "no_colon_here"')
        with pytest.raises(ConfigError, match="package.module:function") as error:
            declared_stimuli(root, SCREEN)
        assert "pyproject.toml" in str(error.value)

    def test_a_module_that_cannot_be_imported_is_named(self, tmp_path):
        root, _ = make_experiment(
            tmp_path / "exp", declaration='stimuli = "{package}.missing:stimulus_images"'
        )
        with pytest.raises(ConfigError, match=r"cannot import preview_experiment_\d+\.missing"):
            declared_stimuli(root, SCREEN)

    def test_a_missing_or_uncallable_function_is_named(self, tmp_path):
        root, package = make_experiment(
            tmp_path / "a", declaration='stimuli = "{package}.stimulus_set:nothing"'
        )
        with pytest.raises(ConfigError, match=f"{package}.stimulus_set has no nothing"):
            declared_stimuli(root, SCREEN)
        root, _ = make_experiment(
            tmp_path / "b",
            "stimulus_images = 42\n",
            'stimuli = "{package}.stimulus_set:stimulus_images"',
        )
        with pytest.raises(ConfigError, match="is not a function but int"):
            declared_stimuli(root, SCREEN)

    @pytest.mark.parametrize(
        ("body", "says"),
        [
            ("return None", "returned NoneType"),
            ("return []", "returned no stimuli"),
            ("return [np.zeros((2, 2))]", "item 0 is not a StimulusImage but ndarray"),
            (
                "return [StimulusImage('Same', np.zeros((2, 2))), "
                "StimulusImage('same', np.ones((2, 2)))]",
                "would be the same file: 'Same' and 'same'",
            ),
            (
                "return [StimulusImage('one', np.zeros((2, 2))), "
                "StimulusImage('two', np.zeros((2, 2), dtype=np.uint8))]",
                "'one' and 'two' are the same image",
            ),
        ],
        ids=["not a list", "empty", "not a StimulusImage", "same file", "same picture"],
    )
    def test_a_set_that_breaks_a_rule_is_refused_naming_the_declaration(self, tmp_path, body, says):
        source = (
            "import numpy as np\nfrom alhazen.stimuli import StimulusImage\n\n"
            f"def stimulus_images(screen):\n    {body}\n"
        )
        root, _ = make_experiment(tmp_path / "exp", source)
        with pytest.raises(ConfigError, match="tool.alhazen") as error:
            declared_stimuli(root, SCREEN)
        assert says in str(error.value)

    def test_the_experiments_own_error_reaches_the_reader_unwrapped(self, tmp_path):
        """A bug in the experiment's function is found by its traceback, so it
        is not dressed up as a configuration problem."""
        source = "def stimulus_images(screen):\n    return 1 / 0\n"
        root, _ = make_experiment(tmp_path / "exp", source)
        with pytest.raises(ZeroDivisionError):
            declared_stimuli(root, SCREEN)


# ----------------------------------------------------------------------
# Writing the folder
# ----------------------------------------------------------------------


class TestWritePreview:
    def test_one_png_per_stimulus_and_the_index_last(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp")
        out = root / "docs" / "stimuli"
        written = write_preview(root, "sim", out)
        assert written == [out / "grey-field.png", out / "red-square.png", out / "README.md"]

        # The rig's scale: 60 cm wide at 60 cm, 1920 px, is 33.51 px per degree.
        screen = Screen.from_monitor(_rig_monitor())
        size = int(round(screen.px_per_deg))
        grey = read_png((out / "grey-field.png").read_bytes())
        assert grey.shape == (size, 2 * size) and np.all(grey == 128)
        red = read_png((out / "red-square.png").read_bytes())
        assert red.shape == (size, size, 3)
        assert np.all(red[..., 0] == 255) and np.all(red[..., 1:] == 0)

    def test_the_index_says_what_each_image_is_and_how_to_draw_them_again(self, tmp_path):
        root, package = make_experiment(tmp_path / "exp")
        out = root / "docs" / "stimuli"
        write_preview(root, "sim", out)
        index = (out / "README.md").read_text(encoding="utf-8")
        assert index.splitlines()[0] == INDEX_MARKER
        # The opening paragraph is wrapped to fit; read it as words.
        words = " ".join(index.split())
        assert "# Demo experiment: every stimulus" in index
        assert f"`{package}.stimulus_set:stimulus_images`" in words
        assert "rig `sim`: 33.51 px per degree, on a 1920 x 1080 px panel" in words
        # As typed in the experiment's folder: a relative path, no machine's.
        assert "    alhazen preview --rig sim --out docs/stimuli\n" in index
        assert str(tmp_path) not in index
        for name, caption in [
            ("grey-field", "a mid-grey field, twice as wide as it is tall"),
            ("red-square", "a red square, one degree on a side"),
        ]:
            assert f"## {name}\n\n{caption}\n" in index
            assert f"![{name}]({name}.png)" in index

    def test_a_folder_outside_the_experiment_is_not_spelled_out(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp")
        write_preview(root, "sim", tmp_path / "elsewhere")
        index = (tmp_path / "elsewhere" / "README.md").read_text(encoding="utf-8")
        assert "--out <this folder>" in index
        assert str(tmp_path) not in index

    def test_drawing_again_into_the_same_folder_replaces_its_own_files(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp")
        out = root / "docs" / "stimuli"
        first = write_preview(root, "sim", out)
        assert write_preview(root, "sim", out) == first

    def test_an_image_the_declaration_no_longer_has_is_refused_and_nothing_written(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp")
        out = root / "docs" / "stimuli"
        out.mkdir(parents=True)
        (out / "old-stimulus.png").write_bytes(b"left over")
        with pytest.raises(ConfigError, match="old-stimulus.png") as error:
            write_preview(root, "sim", out)
        assert "Nothing was written" in str(error.value)
        assert sorted(p.name for p in out.iterdir()) == ["old-stimulus.png"]

    def test_a_readme_somebody_wrote_is_never_overwritten(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp")
        out = root / "docs" / "stimuli"
        out.mkdir(parents=True)
        (out / "README.md").write_text("# Notes I wrote\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not written by alhazen preview"):
            write_preview(root, "sim", out)
        assert (out / "README.md").read_text(encoding="utf-8") == "# Notes I wrote\n"
        assert not list(out.glob("*.png"))

    def test_an_output_that_is_a_file_is_refused(self, tmp_path):
        root, _ = make_experiment(tmp_path / "exp")
        target = root / "taken"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(ConfigError, match="is a file"):
            write_preview(root, "sim", target)

    def test_a_declaration_that_fails_leaves_no_folder_behind(self, tmp_path):
        source = (
            "import numpy as np\nfrom alhazen.stimuli import StimulusImage\n\n"
            "def stimulus_images(screen):\n"
            "    return [StimulusImage('a', np.zeros((2, 2))), 'not an image']\n"
        )
        root, _ = make_experiment(tmp_path / "exp", source)
        with pytest.raises(ConfigError, match="item 1"):
            write_preview(root, "sim", root / "out")
        assert not (root / "out").exists()


def _rig_monitor():
    from alhazen.config.loader import load_rig

    return load_rig(RIG).monitor


# ----------------------------------------------------------------------
# The command
# ----------------------------------------------------------------------


class TestCommand:
    def test_it_prints_every_file_it_wrote(self, tmp_path, capsys):
        root, _ = make_experiment(tmp_path / "exp")
        out = tmp_path / "out"
        status = main(["preview", "--project", str(root), "--rig", "sim", "--out", str(out)])
        assert status == 0
        printed = capsys.readouterr().out.splitlines()
        assert printed == [
            str(out / name) for name in ("grey-field.png", "red-square.png", "README.md")
        ]

    def test_it_runs_in_the_experiments_folder_without_project(self, tmp_path, monkeypatch):
        root, _ = make_experiment(tmp_path / "exp")
        monkeypatch.chdir(root)
        assert main(["preview", "--rig", "sim", "--out", "media"]) == 0
        assert (root / "media" / "grey-field.png").is_file()

    def test_a_folder_that_is_not_an_experiment_is_refused_naming_the_flag(self, tmp_path, capsys):
        status = main(
            ["preview", "--project", str(tmp_path), "--rig", "sim", "--out", str(tmp_path / "x")]
        )
        assert status == 1
        assert "--project" in capsys.readouterr().err

    def test_a_refusal_exits_1_with_the_reason(self, tmp_path, capsys):
        root, _ = make_experiment(tmp_path / "exp", declaration="")
        status = main(
            ["preview", "--project", str(root), "--rig", "sim", "--out", str(tmp_path / "x")]
        )
        assert status == 1
        error = capsys.readouterr().err
        assert error.startswith("CANNOT PREVIEW: ")
        assert "declares no stimuli" in error

    def test_it_takes_no_task_and_no_parameter_file(self, tmp_path):
        """The point of it: the stimuli are the experiment's, so neither is
        an option the reader could set and expect to change the images."""
        root, _ = make_experiment(tmp_path / "exp")
        for flag in ("--task", "--params", "--task-config"):
            with pytest.raises(SystemExit) as exit_info:
                main(
                    [
                        "preview",
                        "--project",
                        str(root),
                        "--rig",
                        "sim",
                        "--out",
                        str(tmp_path / "x"),
                        flag,
                        "y",
                    ]
                )
            assert exit_info.value.code == 2
