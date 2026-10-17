"""Bundles: the platform's unit of storage, checked at every boundary.

A bundle crosses trust boundaries in both directions: an uploaded archive becomes one,
and one becomes files on a rig. These tests pin the format (the same id as the Mana
kit for the same files), the integrity checks (a tampered or truncated bundle is
refused) and the path rules (nothing is written outside its destination).
"""

from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from alhazen.experiment_platform import bundles
from alhazen.experiment_platform.errors import BundleError, InvalidRequest
from alhazen.experiment_platform.safety import check_id, safe_relative

BLOBS = [
    ("README.md", b"# x\n", 0),
    ("run.py", b"print(1)\n", 1),
    ("stim/a.png", b"\x89PNG\0\x01", 0),
]


def built(prefix: str = "demo") -> bundles.Bundle:
    entries, excluded = bundles.split_blobs(BLOBS)
    return bundles.build_bundle(entries, excluded, prefix)


class TestFormat:
    def test_id_matches_the_mana_kit_for_the_same_files(self):
        # hubkit.bundle_id(split_blobs(BLOBS), "demo") on the Mana platform gives this id;
        # the same source must give the same bundle on every host.
        assert built().id == "demo-7226cc1756"

    def test_text_stays_text_and_binaries_are_base64(self):
        entries = built().entries()
        assert entries[0] == ["README.md", "# x\n", 0]
        assert entries[1] == ["run.py", "print(1)\n", 1]
        assert entries[2][3] == "b64" and bundles.entry_bytes(entries[2]) == b"\x89PNG\0\x01"

    def test_order_of_the_source_listing_does_not_change_the_id(self):
        entries, excluded = bundles.split_blobs(list(reversed(BLOBS)))
        assert bundles.build_bundle(entries, excluded, "demo").id == built().id

    def test_meta_describes_the_files(self):
        meta = built().meta
        assert meta["fileCount"] == 3 and meta["binaries"] == 1 and meta["chunkCount"] == 1
        assert json.loads(meta["files"]) == [["README.md", 4], ["run.py", 9], ["stim/a.png", 6]]
        assert meta["sha256"].startswith(built().id.rsplit("-", 1)[1])

    def test_large_binaries_are_excluded_not_truncated(self, monkeypatch):
        monkeypatch.setattr(bundles, "BINARY_FILE_MAX", 4)
        entries, excluded = bundles.split_blobs(BLOBS)
        assert excluded == [["stim/a.png", 6]]
        assert [e[0] for e in entries] == ["README.md", "run.py"]

    def test_many_files_are_split_into_chunks(self, monkeypatch):
        monkeypatch.setattr(bundles, "CHUNK_BYTES", 64)
        blobs = [(f"f{i:02d}.txt", b"x" * 40, 0) for i in range(5)]
        b = bundles.build_bundle(*bundles.split_blobs(blobs), "many")
        assert b.meta["chunkCount"] == 5
        assert [e[0] for e in b.entries()] == [f"f{i:02d}.txt" for i in range(5)]


class TestIntegrity:
    def test_a_changed_file_is_refused(self):
        b = built()
        chunk = json.loads(b.chunks[0])
        chunk[1][1] = "print(2)\n"
        with pytest.raises(BundleError, match="digest|id"):
            bundles.read_entries(b.meta, [json.dumps(chunk)])

    def test_a_missing_chunk_is_refused(self):
        b = built()
        with pytest.raises(BundleError, match="incomplete"):
            bundles.read_entries(b.meta, [])

    def test_a_malformed_entry_is_refused(self):
        b = built()
        with pytest.raises(BundleError, match="malformed"):
            bundles.read_entries(b.meta, [json.dumps([["a.txt", 5, 0]])])

    def test_a_traversal_path_inside_a_stored_chunk_is_refused(self):
        meta = dict(built().meta, files="[]", sha256=None, id=None)
        with pytest.raises(BundleError, match="segment"):
            bundles.read_entries(meta, [json.dumps([["../evil.py", "x", 0]])])

    def test_duplicate_files_are_refused(self):
        with pytest.raises(BundleError, match="twice"):
            bundles.split_blobs([("a.txt", b"1", 0), ("a.txt", b"2", 0)])


class TestPaths:
    @pytest.mark.parametrize(
        "path",
        [
            "../x",
            "a/../../x",
            "/etc/passwd",
            "C:/x",
            "a\\b",
            "a//b",
            "./a",
            ".git/config",
            "a/\x00b",
            "con.txt",
            "a/b ",
            "a/x:y",
            "",
        ],
    )
    def test_unsafe_paths_are_refused(self, path):
        with pytest.raises(BundleError):
            safe_relative(path)

    @pytest.mark.parametrize(
        "path", ["run.py", "configs/task.yaml", "docs/tasks/a-b_c.md", ".gitignore"]
    )
    def test_ordinary_paths_pass(self, path):
        assert safe_relative(path) == path

    @pytest.mark.parametrize("value", ["", "A", "../x", "a/b", "a..b", "-a", "nul", "x" * 81])
    def test_bad_identifiers_are_refused(self, value):
        with pytest.raises(InvalidRequest):
            check_id(value, "Experiment id")


class TestMaterialize:
    def test_files_land_inside_the_destination_with_their_modes(self, tmp_path):
        dest = bundles.materialize(built().entries(), tmp_path / "copy")
        assert (dest / "README.md").read_text() == "# x\n"
        assert (dest / "stim" / "a.png").read_bytes() == b"\x89PNG\0\x01"
        if os.name == "posix":
            assert os.access(dest / "run.py", os.X_OK)

    @pytest.mark.skipif(os.name != "posix", reason="symbolic links need POSIX permissions here")
    def test_a_symlinked_folder_cannot_redirect_a_write(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        dest = tmp_path / "copy"
        dest.mkdir()
        (dest / "stim").symlink_to(outside, target_is_directory=True)
        with pytest.raises(BundleError, match="outside"):
            bundles.materialize(built().entries(), dest)
        assert list(outside.iterdir()) == []


def zip_of(members: dict[str, bytes], links: tuple[str, ...] = ()) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)
            mode = stat.S_IFLNK | 0o777 if name in links else stat.S_IFREG | 0o644
            info.external_attr = mode << 16
            z.writestr(info, data)
    return out.getvalue()


class TestArchives:
    def test_a_round_trip_keeps_every_file(self):
        raw = bundles.zip_entries(built().entries(), "demo")
        blobs = bundles.blobs_from_zip(raw)
        assert [(p, d) for p, d, _ in blobs] == [(p, d) for p, d, _ in BLOBS]
        assert {p: x for p, _, x in blobs}["run.py"] == 1

    def test_one_top_folder_is_stripped_and_caches_are_skipped(self):
        raw = zip_of({"exp/run.py": b"1", "exp/.git/HEAD": b"x", "exp/pkg/__pycache__/a.pyc": b"x"})
        assert [p for p, _, _ in bundles.blobs_from_zip(raw)] == ["run.py"]

    def test_traversal_in_an_archive_is_refused(self):
        with pytest.raises(BundleError):
            bundles.blobs_from_zip(zip_of({"run.py": b"1", "../escape.py": b"2"}))

    def test_symbolic_links_in_an_archive_are_refused(self):
        with pytest.raises(BundleError, match="symbolic link"):
            bundles.blobs_from_zip(
                zip_of({"run.py": b"1", "link": b"/etc/passwd"}, links=("link",))
            )

    def test_an_archive_that_expands_too_far_is_refused(self):
        raw = zip_of({"big.txt": b"0" * 10_000})
        with pytest.raises(BundleError, match="size limit"):
            bundles.blobs_from_zip(raw, max_expanded=1_000)

    def test_not_a_zip_is_refused(self):
        with pytest.raises(BundleError, match="not a zip"):
            bundles.blobs_from_zip(b"plain text")


class TestDirectories:
    def test_a_folder_snapshot_skips_environments_and_data(self, tmp_path: Path):
        (tmp_path / "run.py").write_text("1")
        for skipped in (".venv/lib.py", "data/s1.csv", "pkg/__pycache__/x.pyc"):
            (tmp_path / skipped).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / skipped).write_text("x")
        (tmp_path / "pkg" / "task.py").write_text("2")
        assert [p for p, _, _ in bundles.blobs_from_dir(tmp_path)] == ["pkg/task.py", "run.py"]

    @pytest.mark.skipif(os.name != "posix", reason="symbolic links need POSIX permissions here")
    def test_a_symbolic_link_in_a_folder_is_refused(self, tmp_path: Path):
        (tmp_path / "run.py").write_text("1")
        (tmp_path / "secret").symlink_to("/etc/hostname")
        with pytest.raises(BundleError, match="symbolic link"):
            bundles.blobs_from_dir(tmp_path)
