"""alhazen.hub.packages: the experiment package format and its safety rules.

Every hostile archive here is built inside pytest's tmp_path; nothing is ever
extracted except into tmp_path, and the traversal cases check that nothing
appeared beside it either.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import time
import zipfile
import zlib
from pathlib import Path

import pytest

from alhazen.errors import AlhazenError
from alhazen.hub import packages as pk
from alhazen.hub.packages import (
    MANIFEST_NAME,
    PackageError,
    PackageFile,
    build_bundle,
    compatibility_problems,
    extract_bundle,
    inspect_bundle,
    safe_relative,
    suggest_files,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX links and FIFOs")

META = {
    "name": "demo-experiment",
    "version": "1.2.3",
    "title": "Demo experiment",
    "description": "A synthetic experiment.\nTwo lines.",
    "hardware": {"display": True, "eye_tracker": False, "reward": False},
    "license": "MIT",
    "citations": ["Someone (2020). A paper."],
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_for(members: dict[str, bytes], **overrides) -> dict:
    manifest = {
        "schema_version": 1,
        "entrypoint": "run.py",
        "python_min": "3.10",
        "alhazen_min": "2.13.0",
        "platforms": ["darwin", "linux", "win32"],
        **META,
        "files": [
            {"path": name, "size": len(data), "sha256": sha(data)}
            for name, data in sorted(members.items())
        ],
    }
    manifest.update(overrides)
    return manifest


def write_zip(path: Path, entries: list[tuple[zipfile.ZipInfo | str, bytes]], **kwargs) -> Path:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns on deliberate duplicates
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, **kwargs) as archive:
            for info, data in entries:
                archive.writestr(info, data)
    return path


def raw_bundle(
    path: Path,
    members: dict[str, bytes] | None = None,
    *,
    manifest: dict | bytes | None = None,
    extra: list[tuple[zipfile.ZipInfo | str, bytes]] = (),
    declare: dict[str, bytes] | None = None,
) -> Path:
    """A bundle written member by member: ``members`` are stored and (unless
    ``declare`` says otherwise) declared; ``extra`` members are stored only."""
    members = {"run.py": b"print('run')\n"} if members is None else members
    if manifest is None:
        manifest = manifest_for(members if declare is None else declare)
    body = manifest if isinstance(manifest, bytes) else json.dumps(manifest).encode()
    entries = [(MANIFEST_NAME, body), *members.items(), *extra]
    return write_zip(path, entries)


def central_entries(data: bytes) -> list[tuple[int, str]]:
    """(offset of central record, name) for each member, read by hand."""
    cd_size, cd_offset = struct.unpack_from("<LL", data, len(data) - 22 + 12)
    found, position = [], cd_offset
    while position < cd_offset + cd_size:
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, position + 28)
        name = data[position + 46 : position + 46 + name_len].decode("utf-8", "replace")
        found.append((position, name))
        position += 46 + name_len + extra_len + comment_len
    return found


def patch_member(path: Path, name: str, *, flag_or=0, flag_and=0xFFFF, usize=None, offset=None):
    """Edit one member's header fields in both its central and local record."""
    data = bytearray(path.read_bytes())
    for position, member in central_entries(bytes(data)):
        if member != name:
            continue
        local = struct.unpack_from("<L", data, position + 42)[0]
        for flag_at in (position + 8, local + 6):
            flag = struct.unpack_from("<H", data, flag_at)[0]
            struct.pack_into("<H", data, flag_at, (flag | flag_or) & flag_and)
        if usize is not None:
            struct.pack_into("<L", data, position + 24, usize)
            struct.pack_into("<L", data, local + 22, usize)
        if offset is not None:
            struct.pack_into("<L", data, position + 42, offset)
    path.write_bytes(bytes(data))


def make_source(root: Path) -> Path:
    source = root / "experiment"
    for name, text in {
        "run.py": "print('run')\n",
        "pyproject.toml": "[project]\nname = 'demo'\n",
        "uv.lock": "version = 1\n",
        "README.md": "# Demo\n",
        "src/demo/__init__.py": "VALUE = 1\n",
        "src/demo/data/stimulus.json": "{}\n",
        "configs/task.yaml": "trials: 10\n",
        "docs/experiment.json": '{"methods": "docs/methods.md"}\n',
        "docs/methods.md": "## Methods\n",
        "docs/timeline.svg": "<svg/>\n",
        "configs/rig-lab.yaml": "monitor: {}\n",
        "configs/measurements/rig-lab_2026.json": "{}\n",
        "data/sub-01/trials.csv": "a\n1\n",
        "data-rehearsal/x.csv": "a\n",
        "people/people.sqlite3": "db",
        ".env": "TOKEN=1\n",
        ".venv/pyvenv.cfg": "home = /usr\n",
        ".venv/lib/x.py": "",
        "src/demo/__pycache__/x.cpython-311.pyc": "",
        ".DS_Store": "",
        "keys/id_ed25519": "key",
    }.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return source


CLEAN = [
    "README.md",
    "configs/task.yaml",
    "docs/experiment.json",
    "docs/methods.md",
    "docs/timeline.svg",
    "pyproject.toml",
    "run.py",
    "src/demo/__init__.py",
    "src/demo/data/stimulus.json",
    "uv.lock",
]


@pytest.fixture
def source(tmp_path):
    return make_source(tmp_path)


@pytest.fixture
def bundle(tmp_path, source):
    return build_bundle(source, tmp_path / "demo.zip", dict(META), CLEAN), tmp_path / "demo.zip"


# --------------------------------------------------------------------------
# Paths


class TestSafeRelative:
    @pytest.mark.parametrize(
        "path", ["run.py", "src/pkg/__init__.py", "données/é.txt", "a b/c-d_e.f", ".github/x.yml"]
    )
    def test_portable_paths_pass_unchanged(self, path):
        assert safe_relative(path) == path

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/etc/passwd",
            "//server/share/x",
            "../x",
            "a/../b",
            "a/./b",
            "./a",
            "a//b",
            "a/",
            "a\\b",
            "..\\..\\x",
            "C:/x.py",
            "C:x.py",
            "file.txt:stream",
            "x\x00y",
            "x\ny",
            "x\x7f",
            "e\u0301.txt",  # NFD
            "\u202etxt.py",  # bidi override
            "ru\u200bn.py",  # zero-width space
            "a\u00a0b",  # non-breaking space
            "CON",
            "con.txt",
            "src/NUL.py",
            "lpt1",
            "COM9.log",
            "conin$",
            "name.",
            "name ",
            " name",
            "a" * 256,
            "a/" * 600 + "b",
            "x?.py",
            'x".py',
        ],
    )
    def test_unportable_paths_are_refused(self, path):
        with pytest.raises(PackageError):
            safe_relative(path)

    def test_non_text_is_refused(self):
        with pytest.raises(PackageError, match="text"):
            safe_relative(3)  # type: ignore[arg-type]

    def test_package_error_is_a_value_error_and_an_alhazen_error(self):
        assert issubclass(PackageError, ValueError)
        assert issubclass(PackageError, AlhazenError)


# --------------------------------------------------------------------------
# Building and reading


class TestBuildAndInspect:
    def test_round_trip(self, bundle):
        info, path = bundle
        data = path.read_bytes()
        assert info.sha256 == sha(data) and info.size == len(data)
        assert inspect_bundle(path) == info
        manifest = info.manifest
        assert manifest["schema_version"] == 1 and manifest["entrypoint"] == "run.py"
        assert manifest["python_min"] == "3.10" and manifest["alhazen_min"] == "2.13.0"
        assert manifest["platforms"] == ["darwin", "linux", "win32"]
        assert [entry["path"] for entry in manifest["files"]] == CLEAN
        source = path.parent / "experiment"
        for entry in manifest["files"]:
            content = (source / entry["path"]).read_bytes()
            assert entry == {"path": entry["path"], "size": len(content), "sha256": sha(content)}
        assert info.files[0] == PackageFile("README.md", len(b"# Demo\n"), sha(b"# Demo\n"))
        with zipfile.ZipFile(path) as archive:
            assert archive.namelist() == [MANIFEST_NAME, *CLEAN]

    def test_manifest_copy_cannot_alter_a_later_read(self, bundle):
        info, path = bundle
        info.manifest["files"].clear()
        assert inspect_bundle(path).manifest["files"]

    def test_build_is_deterministic(self, tmp_path, source):
        first = build_bundle(source, tmp_path / "a.zip", dict(META), CLEAN)
        os.utime(source / "run.py", (1, 1))
        second = build_bundle(source, tmp_path / "b.zip", dict(META), list(reversed(CLEAN)))
        assert first.sha256 == second.sha256
        assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()

    def test_optional_documentation_pointer(self, tmp_path, source):
        meta = {**META, "documentation": "docs/experiment.json"}
        info = build_bundle(source, tmp_path / "d.zip", meta, CLEAN)
        assert info.manifest["documentation"] == "docs/experiment.json"
        assert (
            "documentation"
            not in build_bundle(source, tmp_path / "e.zip", dict(META), CLEAN).manifest
        )

    @pytest.mark.parametrize(
        ("pointer", "match"),
        [
            ("docs/missing.json", "not a declared file"),
            ("docs/methods.md", "must name a .json"),
            ("../experiment.json", "documentation"),
            ("/docs/experiment.json", "documentation"),
            (7, "documentation"),
        ],
    )
    def test_documentation_pointer_must_be_a_declared_json_file(
        self, tmp_path, source, pointer, match
    ):
        with pytest.raises(PackageError, match=match):
            build_bundle(source, tmp_path / "d.zip", {**META, "documentation": pointer}, CLEAN)
        assert not (tmp_path / "d.zip").exists()

    def test_documentation_size_is_bounded(self, tmp_path, source):
        (source / "docs" / "experiment.json").write_bytes(b" " * (pk.MAX_DOCUMENTATION_BYTES + 1))
        with pytest.raises(PackageError, match="larger than"):
            build_bundle(
                source, tmp_path / "d.zip", {**META, "documentation": "docs/experiment.json"}, CLEAN
            )

    def test_file_systems_without_hard_links_get_an_exclusive_copy(
        self, tmp_path, source, monkeypatch
    ):
        def no_links(*args, **kwargs):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(pk.os, "link", no_links)
        info = build_bundle(source, tmp_path / "o.zip", dict(META), CLEAN)
        assert sha((tmp_path / "o.zip").read_bytes()) == info.sha256
        assert sorted(os.listdir(tmp_path)) == ["experiment", "o.zip"]

    def test_build_refuses_an_existing_output_and_leaves_it(self, tmp_path, source):
        (tmp_path / "out.zip").write_bytes(b"mine")
        with pytest.raises(PackageError, match="already exists"):
            build_bundle(source, tmp_path / "out.zip", dict(META), CLEAN)
        assert (tmp_path / "out.zip").read_bytes() == b"mine"

    @pytest.mark.parametrize(
        "path",
        [
            ".env",
            "configs/prod.env",
            "configs/.env.local",
            "data/sub-01/trials.csv",
            "data-rehearsal/x.csv",
            "people/people.sqlite3",
            "configs/rig-lab.yaml",
            "configs/measurements/rig-lab_2026.json",
            "keys/id_ed25519",
            ".venv/lib/x.py",
            "src/demo/__pycache__/x.cpython-311.pyc",
            ".git/config",
            "certs/server.pem",
            "credentials.json",
            "configs/client_secret_123.json",
            "hub-token.txt",
            "sub-02/x.txt",
            "participants.tsv",
            "recordings/s1.edf",
            "experiment.sqlite3-wal",
            ".alhazen-write-check",
            "alhazen-package.json",
        ],
    )
    def test_sensitive_files_are_refused(self, tmp_path, source, path):
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("x", encoding="utf-8")
        with pytest.raises(PackageError, match="may not be packaged"):
            build_bundle(source, tmp_path / "out.zip", dict(META), [*CLEAN, path])
        assert sorted(os.listdir(tmp_path)) == ["experiment"]

    def test_env_templates_and_code_data_folders_are_allowed(self, tmp_path, source):
        (source / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
        (source / "data_analysis").mkdir()
        (source / "data_analysis" / "fit.py").write_text("", encoding="utf-8")
        info = build_bundle(
            source, tmp_path / "o.zip", dict(META), [*CLEAN, ".env.example", "data_analysis/fit.py"]
        )
        assert ".env.example" in [entry["path"] for entry in info.manifest["files"]]

    def test_a_folder_holding_pyvenv_cfg_is_an_environment_whatever_its_name(
        self, tmp_path, source
    ):
        (source / "tools" / "env" / "lib").mkdir(parents=True)
        (source / "tools" / "env" / "pyvenv.cfg").write_text("home=x\n", encoding="utf-8")
        (source / "tools" / "env" / "lib" / "m.py").write_text("", encoding="utf-8")
        with pytest.raises(PackageError, match="environment"):
            build_bundle(source, tmp_path / "o.zip", dict(META), [*CLEAN, "tools/env/lib/m.py"])

    @pytest.mark.parametrize(
        ("files", "match"),
        [
            (["README.md"], "run.py"),
            ([*CLEAN, "run.py"], "twice"),
            ([*CLEAN, "Run.py"], "differ only by case"),
            ([*CLEAN, "SRC/demo/x.py"], "spelled"),
            ([*CLEAN, "../outside.py"], "step up"),
            ([*CLEAN, "missing.py"], "does not exist"),
            ("run.py", "list"),
        ],
    )
    def test_bad_file_lists_are_refused(self, tmp_path, source, files, match):
        (source / "SRC").mkdir(exist_ok=True) if sys.platform == "linux" else None
        with pytest.raises(PackageError, match=match):
            build_bundle(source, tmp_path / "o.zip", dict(META), files)
        assert not (tmp_path / "o.zip").exists()

    @pytest.mark.parametrize(
        ("change", "match"),
        [
            ({"name": "Demo_Experiment"}, "name"),
            ({"version": "1.0"}, "version"),
            ({"version": "1.0.0-rc1"}, "version"),
            ({"title": ""}, "title"),
            ({"title": "x\u202ey"}, "title"),
            ({"description": 5}, "description"),
            ({"entrypoint": "main.py"}, "entrypoint"),
            ({"python_min": "2.7"}, "python_min"),
            ({"python_min": "3.9"}, "python_min"),
            ({"alhazen_min": "2.12.0"}, "alhazen_min"),
            ({"platforms": []}, "platforms"),
            ({"platforms": ["linux", "linux"]}, "twice"),
            ({"platforms": ["freebsd"]}, "platforms"),
            ({"hardware": {"display": True}}, "hardware"),
            ({"hardware": {"display": 1, "eye_tracker": False, "reward": False}}, "hardware"),
            ({"license": ""}, "license"),
            ({"citations": "one"}, "citations"),
            ({"citations": [3]}, "citations"),
            ({"files": []}, "computed"),
            ({"schema_version": 2}, "computed"),
            ({"homepage": "x"}, "unknown"),
        ],
    )
    def test_bad_metadata_is_refused(self, tmp_path, source, change, match):
        with pytest.raises(PackageError, match=match):
            build_bundle(source, tmp_path / "o.zip", {**META, **change}, CLEAN)
        assert sorted(os.listdir(tmp_path)) == ["experiment"]

    def test_missing_metadata_is_refused(self, tmp_path, source):
        meta = dict(META)
        del meta["hardware"]
        with pytest.raises(PackageError, match="missing hardware"):
            build_bundle(source, tmp_path / "o.zip", meta, CLEAN)

    def test_a_file_that_changes_between_reads_is_refused(self, tmp_path, source, monkeypatch):
        original = pk._read_source

        def racing(root, path, sink=None):
            result = original(root, path, sink)
            if sink is None and path == "run.py":
                (root / "run.py").write_text("print('changed!')\n", encoding="utf-8")
            return result

        monkeypatch.setattr(pk, "_read_source", racing)
        with pytest.raises(PackageError, match="changed while"):
            build_bundle(source, tmp_path / "o.zip", dict(META), CLEAN)
        assert sorted(os.listdir(tmp_path)) == ["experiment"]

    @POSIX_ONLY
    def test_links_and_special_files_are_refused(self, tmp_path, source):
        os.symlink(source / "run.py", source / "alias.py")
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / "secret.py").write_text("x", encoding="utf-8")
        os.symlink(tmp_path / "elsewhere", source / "linked")
        os.mkfifo(source / "pipe")
        for path in ("alias.py", "linked/secret.py", "pipe"):
            with pytest.raises(PackageError, match="link|not a regular file|not a folder"):
                build_bundle(source, tmp_path / "o.zip", dict(META), [*CLEAN, path])
        assert not (tmp_path / "o.zip").exists()

    def test_errors_do_not_reveal_local_paths(self, tmp_path, source):
        for call in (
            lambda: inspect_bundle(tmp_path / "missing.zip"),
            lambda: build_bundle(source, tmp_path / "o.zip", dict(META), [*CLEAN, "nope.py"]),
        ):
            with pytest.raises(PackageError) as caught:
                call()
            assert str(tmp_path) not in str(caught.value)


# --------------------------------------------------------------------------
# Hostile and malformed archives


def good_members() -> dict[str, bytes]:
    return {"run.py": b"print('run')\n", "src/m.py": b"X = 1\n"}


class TestInspectRefuses:
    def test_a_well_formed_raw_bundle_passes(self, tmp_path):
        info = inspect_bundle(raw_bundle(tmp_path / "b.zip", good_members()))
        assert [entry["path"] for entry in info.manifest["files"]] == ["run.py", "src/m.py"]

    def test_streamed_archives_with_data_descriptors_are_refused(self, tmp_path):
        """Contract change (review PB1.4): packages use the small ZIP subset
        build_bundle writes, so a streamed archive, whose members put sizes
        and CRC in a trailing descriptor, is refused rather than guessed at."""

        class Unseekable(io.RawIOBase):
            def __init__(self):
                self.buffer = bytearray()

            def writable(self):
                return True

            def write(self, data):
                self.buffer += data
                return len(data)

        members = good_members()
        stream = Unseekable()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest_for(members)))
            for name, data in members.items():
                with archive.open(name, "w") as writer:
                    writer.write(data)
        (tmp_path / "s.zip").write_bytes(bytes(stream.buffer))
        with zipfile.ZipFile(tmp_path / "s.zip") as archive:
            assert all(info.flag_bits & 0x08 for info in archive.infolist())
        with pytest.raises(PackageError, match="data descriptor"):
            inspect_bundle(tmp_path / "s.zip")

    @pytest.mark.parametrize(
        "name",
        [
            "../evil.py",
            "/abs/evil.py",
            "a\\..\\evil.py",
            "C:/evil.py",
            "src/../../evil.py",
            "nul.py",
        ],
    )
    def test_unsafe_member_names(self, tmp_path, name):
        members = {**good_members(), name: b"bad"}
        path = raw_bundle(tmp_path / "b.zip", members)
        with pytest.raises(PackageError):
            inspect_bundle(path)
        target = tmp_path / "install"
        with pytest.raises(PackageError):
            extract_bundle(path, target)
        assert not target.exists()
        assert not (tmp_path.parent / "evil.py").exists()
        assert sorted(os.listdir(tmp_path)) == ["b.zip"]

    def test_duplicate_members(self, tmp_path):
        path = raw_bundle(tmp_path / "b.zip", good_members(), extra=[("run.py", b"other")])
        with pytest.raises(PackageError, match="twice"):
            inspect_bundle(path)

    @pytest.mark.parametrize(
        ("extra_name", "match"),
        [("RUN.py", "case"), ("SRC/n.py", "spelled"), ("src", "folder")],
    )
    def test_colliding_members(self, tmp_path, extra_name, match):
        members = {**good_members(), extra_name: b"x"}
        with pytest.raises(PackageError, match=match):
            inspect_bundle(raw_bundle(tmp_path / "b.zip", members))

    def test_folder_entries(self, tmp_path):
        path = raw_bundle(tmp_path / "b.zip", good_members(), extra=[("src/", b"")])
        with pytest.raises(PackageError, match="folder entry"):
            inspect_bundle(path)

    @pytest.mark.parametrize(
        "mode",
        [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o644, stat.S_IFCHR | 0o644, stat.S_IFDIR | 0o755],
    )
    def test_links_and_special_members(self, tmp_path, mode):
        info = zipfile.ZipInfo("link.py")
        info.create_system = 3
        info.external_attr = mode << 16
        members = good_members()
        path = raw_bundle(
            tmp_path / "b.zip",
            members,
            extra=[(info, b"/etc/passwd")],
            declare={**members, "link.py": b"/etc/passwd"},
        )
        with pytest.raises(PackageError, match="link, folder or device"):
            inspect_bundle(path)

    def test_dos_folder_attribute(self, tmp_path):
        info = zipfile.ZipInfo("x.py")
        info.create_system = 0
        info.external_attr = 0x10
        members = good_members()
        path = raw_bundle(
            tmp_path / "b.zip", members, extra=[(info, b"x")], declare={**members, "x.py": b"x"}
        )
        with pytest.raises(PackageError, match="folder or a link"):
            inspect_bundle(path)

    @pytest.mark.parametrize("bit", [0x0001, 0x0040, 0x2000])
    def test_encrypted_members(self, tmp_path, bit):
        path = raw_bundle(tmp_path / "b.zip", good_members())
        patch_member(path, "src/m.py", flag_or=bit)
        with pytest.raises(PackageError, match="encrypted"):
            inspect_bundle(path)

    @pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
    def test_other_compression_methods(self, tmp_path, method):
        info = zipfile.ZipInfo("x.py")
        info.compress_type = method
        members = good_members()
        path = raw_bundle(
            tmp_path / "b.zip", members, extra=[(info, b"x")], declare={**members, "x.py": b"x"}
        )
        with pytest.raises(PackageError, match="compression method"):
            inspect_bundle(path)

    def test_zip64(self, tmp_path):
        members = good_members()
        with zipfile.ZipFile(tmp_path / "b.zip", "w") as archive:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest_for(members)))
            for name, data in members.items():
                with archive.open(name, "w", force_zip64=True) as writer:
                    writer.write(data)
        with pytest.raises(PackageError, match="zip64"):
            inspect_bundle(tmp_path / "b.zip")

    def test_unlisted_member(self, tmp_path):
        path = raw_bundle(tmp_path / "b.zip", good_members(), extra=[("hidden.py", b"x")])
        with pytest.raises(PackageError, match="does not declare"):
            inspect_bundle(path)

    def test_missing_member(self, tmp_path):
        members = good_members()
        path = raw_bundle(tmp_path / "b.zip", {"run.py": members["run.py"]}, declare=members)
        with pytest.raises(PackageError, match="lacks"):
            inspect_bundle(path)

    def test_tampered_bytes(self, tmp_path):
        members = good_members()
        path = raw_bundle(tmp_path / "b.zip", {**members, "src/m.py": b"X = 2\n"}, declare=members)
        with pytest.raises(PackageError, match="SHA-256"):
            inspect_bundle(path)

    def test_wrong_declared_size(self, tmp_path):
        members = good_members()
        manifest = manifest_for(members)
        manifest["files"][1]["size"] += 1
        with pytest.raises(PackageError, match="size"):
            inspect_bundle(raw_bundle(tmp_path / "b.zip", members, manifest=manifest))

    def test_member_longer_than_its_header_says(self, tmp_path):
        """A header that understates a member's size cannot smuggle extra
        bytes: reading stops at the declared size and the CRC fails."""
        long = b"A" * 1000
        declared = {"run.py": b"print('run')\n", "big.txt": long[:10]}
        path = raw_bundle(
            tmp_path / "b.zip", {"run.py": declared["run.py"], "big.txt": long}, declare=declared
        )
        patch_member(path, "big.txt", usize=10)
        with pytest.raises(PackageError):
            inspect_bundle(path)

    def test_overlapping_members(self, tmp_path):
        path = raw_bundle(tmp_path / "b.zip", good_members())
        offsets = {
            name: struct.unpack_from("<L", path.read_bytes(), position + 42)[0]
            for position, name in central_entries(path.read_bytes())
        }
        patch_member(path, "src/m.py", offset=offsets["run.py"])
        with pytest.raises(PackageError):
            inspect_bundle(path)

    def test_hidden_bytes_between_members_and_directory(self, tmp_path):
        """Bytes that belong to no member (a place to smuggle content past
        the manifest) are refused even when every member is valid."""
        path = raw_bundle(tmp_path / "b.zip", good_members())
        data = path.read_bytes()
        cd_size, cd_offset = struct.unpack_from("<LL", data, len(data) - 10)
        hidden = b"hidden payload!!"
        end = bytearray(data[cd_offset:])
        struct.pack_into("<L", end, len(end) - 6, cd_offset + len(hidden))
        path.write_bytes(data[:cd_offset] + hidden + bytes(end))
        with zipfile.ZipFile(path) as archive:  # zipfile itself accepts it
            assert archive.testzip() is None
        with pytest.raises(PackageError, match="no member accounts for"):
            inspect_bundle(path)

    def test_prepended_data(self, tmp_path):
        path = raw_bundle(tmp_path / "b.zip", good_members())
        path.write_bytes(b"#!/bin/sh\nexit 0\n" + path.read_bytes())
        with pytest.raises(PackageError):
            inspect_bundle(path)

    def test_trailing_data_and_comments(self, tmp_path):
        path = raw_bundle(tmp_path / "b.zip", good_members())
        original = path.read_bytes()
        path.write_bytes(original + b"x")
        with pytest.raises(PackageError, match="end record"):
            inspect_bundle(path)
        with zipfile.ZipFile(tmp_path / "c.zip", "w") as archive:
            archive.comment = b"hello"
            archive.writestr(MANIFEST_NAME, json.dumps(manifest_for({"run.py": b"r"})))
            archive.writestr("run.py", b"r")
        with pytest.raises(PackageError):
            inspect_bundle(tmp_path / "c.zip")

    @pytest.mark.parametrize("content", [b"", b"not a zip at all", b"PK\x05\x06" + b"\0" * 18])
    def test_not_an_archive(self, tmp_path, content):
        (tmp_path / "b.zip").write_bytes(content)
        with pytest.raises(PackageError):
            inspect_bundle(tmp_path / "b.zip")

    def test_truncated_archive(self, tmp_path):
        path = raw_bundle(tmp_path / "b.zip", good_members())
        path.write_bytes(path.read_bytes()[:-30])
        with pytest.raises(PackageError):
            inspect_bundle(path)

    def test_name_not_marked_utf8(self, tmp_path):
        members = {"run.py": b"r", "é.py": b"e"}
        path = raw_bundle(tmp_path / "b.zip", members)
        patch_member(path, "é.py", flag_and=0xFFFF ^ 0x0800)
        with pytest.raises(PackageError):
            inspect_bundle(path)

    def test_not_a_regular_file(self, tmp_path):
        with pytest.raises(PackageError, match="regular file"):
            inspect_bundle(tmp_path)

    def test_member_count_is_capped_before_parsing(self, tmp_path):
        members = {"run.py": b"r", **{f"f{i}.py": b"" for i in range(5)}}
        with pytest.raises(PackageError, match="members"):
            inspect_bundle(raw_bundle(tmp_path / "b.zip", members), max_files=3)

    def test_archive_size_is_capped(self, tmp_path):
        path = raw_bundle(tmp_path / "b.zip", good_members())
        with pytest.raises(PackageError, match="larger than"):
            inspect_bundle(path, max_archive_bytes=100)

    def test_expansion_is_capped_before_decompression(self, tmp_path):
        """A 64 MiB file of zeros compresses to about 64 KiB; with a 1 MiB
        expansion cap it is refused from the declared sizes alone."""
        zeros = bytes(64 * 1024 * 1024)
        path = raw_bundle(tmp_path / "b.zip", {"run.py": b"r", "zeros.bin": zeros})
        assert path.stat().st_size < 1024 * 1024
        with pytest.raises(PackageError, match="expands|from 0 to"):
            inspect_bundle(path, max_expanded_bytes=1024 * 1024)

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, 2**40])
    def test_limit_arguments_are_checked(self, tmp_path, value):
        with pytest.raises(ValueError):
            inspect_bundle(tmp_path / "x.zip", max_files=value)


class TestManifestParsing:
    def bundle_with(self, tmp_path, manifest):
        return raw_bundle(tmp_path / "b.zip", {"run.py": b"r"}, manifest=manifest)

    def base(self):
        return manifest_for({"run.py": b"r"})

    @pytest.mark.parametrize(
        "body",
        [
            b"{not json",
            b"[]",
            b"\xef\xbb\xbf{}",
            b"\xff\xfe",
            b'{"a": 1, "a": 2}',
            b'{"size": NaN}',
            b'{"size": 1e999}',
            b"[" * 100_000 + b"]" * 100_000,
            b'{"x": ' + b"9" * 5000 + b"}",
        ],
        ids=[
            "broken",
            "array",
            "bom",
            "not-utf8",
            "duplicate-key",
            "nan",
            "huge-float",
            "deep",
            "huge-int",
        ],
    )
    def test_malformed_json(self, tmp_path, body):
        with pytest.raises(PackageError):
            inspect_bundle(self.bundle_with(tmp_path, body))

    @pytest.mark.parametrize(
        "change",
        [
            {"schema_version": 2},
            {"schema_version": True},
            {"schema_version": 1.0},
            {"entrypoint": "main.py"},
            {"extra": 1},
            {"documentation": "docs/x.json"},
        ],
    )
    def test_bad_fields(self, tmp_path, change):
        with pytest.raises(PackageError):
            inspect_bundle(self.bundle_with(tmp_path, {**self.base(), **change}))

    @pytest.mark.parametrize(
        "entry_change",
        [{"size": True}, {"size": 1.0}, {"size": -1}, {"sha256": "A" * 64}, {"extra": 1}],
    )
    def test_bad_file_entries(self, tmp_path, entry_change):
        manifest = self.base()
        manifest["files"][0] = {**manifest["files"][0], **entry_change}
        with pytest.raises(PackageError):
            inspect_bundle(self.bundle_with(tmp_path, manifest))

    def test_manifest_size_is_capped(self, tmp_path):
        manifest = self.base()
        manifest["description"] = "x" * 100
        body = json.dumps(manifest).encode() + b" " * (pk.MAX_MANIFEST_BYTES + 1)
        with pytest.raises(PackageError, match="larger than"):
            inspect_bundle(self.bundle_with(tmp_path, body))

    def test_manifest_must_be_at_the_root(self, tmp_path):
        members = {"run.py": b"r"}
        write_zip(
            tmp_path / "b.zip",
            [
                ("sub/" + MANIFEST_NAME, json.dumps(manifest_for(members)).encode()),
                ("run.py", b"r"),
            ],
        )
        with pytest.raises(PackageError):
            inspect_bundle(tmp_path / "b.zip")


# --------------------------------------------------------------------------
# Installing


class TestExtract:
    def test_installs_exactly_the_declared_files(self, tmp_path, bundle):
        info, path = bundle
        target = tmp_path / "installed" / "demo"
        target.parent.mkdir()
        assert extract_bundle(path, target) == info
        installed = sorted(
            p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()
        )
        assert installed == CLEAN
        for entry in info.manifest["files"]:
            assert sha((target / entry["path"]).read_bytes()) == entry["sha256"]
        assert os.listdir(target.parent) == ["demo"]  # no staging left behind
        if sys.platform != "win32":
            assert not any(
                os.stat(p).st_mode & (stat.S_IXUSR | stat.S_ISUID)
                for p in target.rglob("*")
                if p.is_file()
            )

    @pytest.mark.parametrize("kind", ["empty folder", "file", "broken link"])
    def test_refuses_an_existing_destination(self, tmp_path, bundle, kind):
        _, path = bundle
        target = tmp_path / "demo"
        if kind == "empty folder":
            target.mkdir()
        elif kind == "file":
            target.write_text("mine", encoding="utf-8")
        else:
            if sys.platform == "win32":
                pytest.skip("needs POSIX links")
            os.symlink(tmp_path / "nowhere", target)
        before = sorted(os.listdir(tmp_path))
        with pytest.raises(PackageError, match="already exists"):
            extract_bundle(path, target)
        assert sorted(os.listdir(tmp_path)) == before
        assert os.path.lexists(target)

    def test_a_second_install_to_the_same_place_is_refused(self, tmp_path, bundle):
        _, path = bundle
        extract_bundle(path, tmp_path / "demo")
        with pytest.raises(PackageError, match="already exists"):
            extract_bundle(path, tmp_path / "demo")

    def test_a_refused_bundle_leaves_nothing(self, tmp_path):
        members = good_members()
        path = raw_bundle(tmp_path / "b.zip", {**members, "src/m.py": b"evil"}, declare=members)
        with pytest.raises(PackageError):
            extract_bundle(path, tmp_path / "demo")
        assert sorted(os.listdir(tmp_path)) == ["b.zip"]

    def test_missing_parent(self, tmp_path, bundle):
        _, path = bundle
        with pytest.raises(PackageError, match="does not exist"):
            extract_bundle(path, tmp_path / "no" / "demo")

    def test_a_destination_changed_during_install_is_kept(self, tmp_path, bundle, monkeypatch):
        _, path = bundle
        target = tmp_path / "demo"
        original = pk._extract_verified

        def meddle(*args):
            original(*args)
            (target / "theirs.txt").write_text("someone else's", encoding="utf-8")

        monkeypatch.setattr(pk, "_extract_verified", meddle)
        with pytest.raises(PackageError, match="changed by something else"):
            extract_bundle(path, target)
        assert os.listdir(target) == ["theirs.txt"]
        assert sorted(os.listdir(tmp_path)) == sorted(["demo", "demo.zip", "experiment"])

    def test_the_package_is_copied_before_it_is_checked(self, tmp_path, bundle, monkeypatch):
        """The checks and the extraction read a private copy, so a source
        file swapped after the copy changes nothing that is installed."""
        info, path = bundle
        original = pk._copy_private

        def swap_after_copy(source, fd, limit):
            result = original(source, fd, limit)
            source.write_bytes(b"swapped")
            return result

        monkeypatch.setattr(pk, "_copy_private", swap_after_copy)
        assert extract_bundle(path, tmp_path / "demo") == info


# --------------------------------------------------------------------------
# Suggesting files


class TestSuggest:
    def test_plain_folder(self, source):
        assert suggest_files(source) == CLEAN

    @pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX links")
    def test_links_are_never_suggested(self, tmp_path, source):
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "x.py").write_text("x", encoding="utf-8")
        os.symlink(tmp_path / "outside", source / "linked")
        os.symlink(source / "run.py", source / "alias.py")
        assert suggest_files(source) == CLEAN

    def test_suggestions_build(self, tmp_path, source):
        info = build_bundle(source, tmp_path / "s.zip", dict(META), suggest_files(source))
        assert [entry["path"] for entry in info.manifest["files"]] == CLEAN

    @pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
    def test_git_tree_suggests_only_tracked_files(self, tmp_path, source):
        def git(*args):
            subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)

        git("init", "-q")
        git("add", "--", *CLEAN[:-1], ".env", "configs/rig-lab.yaml")
        (source / "untracked.py").write_text("x", encoding="utf-8")
        if sys.platform != "win32":
            os.symlink("run.py", source / "alias.py")
            git("add", "alias.py")
        assert suggest_files(source) == CLEAN[:-1]

    def test_missing_folder(self, tmp_path):
        with pytest.raises(PackageError):
            suggest_files(tmp_path / "missing")


class TestCompatibility:
    def manifest(self):
        return manifest_for({"run.py": b"r"}, python_min="3.11", platforms=["linux"])

    def test_compatible(self):
        assert (
            compatibility_problems(
                self.manifest(), python_version=(3, 12), alhazen_version="2.13.0", platform="linux"
            )
            == []
        )

    def test_dev_build_counts_as_its_release(self):
        assert (
            compatibility_problems(
                self.manifest(),
                python_version=(3, 11),
                alhazen_version="2.13.0.dev4+g1",
                platform="linux",
            )
            == []
        )

    def test_each_problem_is_reported(self):
        problems = compatibility_problems(
            self.manifest(), python_version=(3, 10), alhazen_version="2.12.0", platform="win32"
        )
        assert len(problems) == 3
        assert "Python 3.11" in problems[0] and "2.12.0" in problems[1] and "win32" in problems[2]

    def test_unknown_alhazen_version_is_a_problem(self):
        problems = compatibility_problems(
            self.manifest(), python_version=(3, 11), alhazen_version="unknown", platform="linux"
        )
        assert problems and "unknown" in problems[0]


# --------------------------------------------------------------------------
# Regressions from the package review of b112a62 (ids PB1, PM1-PM4, m1-m6)


def craft(members: list[dict]) -> bytes:
    """A ZIP written byte by byte, so one field at a time can be wrong.
    Each member: name, data, and optionally method (0/8), raw (compressed
    bytes), crc, file_size, cextra, lextra, lcrc, lsize, lcsize, flags,
    version."""
    out, directory = bytearray(), bytearray()
    for m in members:
        name = m["name"].encode("utf-8")
        data = m["data"]
        method = m.get("method", 0)
        raw = m.get("raw")
        if raw is None:
            if method == 8:
                packer = zlib.compressobj(6, zlib.DEFLATED, -15)
                raw = packer.compress(data) + packer.flush()
            else:
                raw = data
        crc = m.get("crc", zlib.crc32(data))
        size = m.get("file_size", len(data))
        flags = m.get("flags", 0x800 if not m["name"].isascii() else 0)
        version = m.get("version", 20)
        lextra, cextra = m.get("lextra", b""), m.get("cextra", b"")
        offset = len(out)
        out += struct.pack(
            "<4s5H3L2H",
            b"PK\x03\x04",
            version,
            flags,
            method,
            0,
            0x21,
            m.get("lcrc", crc),
            m.get("lcsize", len(raw)),
            m.get("lsize", size),
            len(name),
            len(lextra),
        )
        out += name + lextra + raw
        directory += struct.pack(
            "<4s6H3L5H2L",
            b"PK\x01\x02",
            (3 << 8) | 20,
            version,
            flags,
            method,
            0,
            0x21,
            crc,
            len(raw),
            size,
            len(name),
            len(cextra),
            0,
            0,
            0,
            (stat.S_IFREG | 0o644) << 16,
            offset,
        )
        directory += name + cextra
    start = len(out)
    out += directory
    count = len(members)
    out += struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, count, count, len(directory), start, 0)
    return bytes(out)


RUN = b"print('declared')\n"


def crafted(tmp_path: Path, run_member: dict | None = None, *, extra=(), name="c.zip") -> Path:
    """A crafted bundle declaring only run.py (= RUN); ``run_member`` holds
    the field overrides for run.py's member."""
    manifest = json.dumps(manifest_for({"run.py": RUN})).encode()
    members = [
        {"name": MANIFEST_NAME, "data": manifest},
        {"name": "run.py", "data": RUN, **(run_member or {})},
        *extra,
    ]
    path = tmp_path / name
    path.write_bytes(craft(members))
    return path


def unicode_path_extra(original: str, other: str) -> bytes:
    body = struct.pack("<BL", 1, zlib.crc32(original.encode())) + other.encode()
    return struct.pack("<HH", 0x7075, len(body)) + body


class TestStrictZipSubset:
    """PB1: every reader and every Python version must see the same names
    and bytes, so anything outside the small subset is refused."""

    def test_the_crafted_baseline_passes(self, tmp_path):
        assert inspect_bundle(crafted(tmp_path)).manifest["files"][0]["path"] == "run.py"
        deflated = crafted(tmp_path, {"method": 8}, name="d.zip")
        assert inspect_bundle(deflated).manifest["name"] == META["name"]

    def test_hidden_local_entry_inside_a_stored_member(self, tmp_path):
        """R1: the directory's compressed size covers an embedded local
        header for evil.py that sequential readers would list."""
        evil = b"import os\n"
        hidden = (
            struct.pack(
                "<4s5H3L2H", b"PK\x03\x04", 20, 0, 0, 0, 0x21, zlib.crc32(evil), 10, 10, 7, 0
            )
            + b"evil.py"
            + evil
        )
        path = crafted(tmp_path, {"raw": RUN + hidden, "lcsize": len(RUN)})
        with pytest.raises(PackageError):
            inspect_bundle(path)

    def test_bytes_after_the_end_of_a_deflate_stream(self, tmp_path):
        """R2"""
        packer = zlib.compressobj(6, zlib.DEFLATED, -15)
        raw = packer.compress(RUN) + packer.flush()
        path = crafted(tmp_path, {"method": 8, "raw": raw + b"X" * 4096})
        with pytest.raises(PackageError, match="after the end|end exactly"):
            inspect_bundle(path)

    def test_a_deflate_stream_cut_short(self, tmp_path):
        packer = zlib.compressobj(6, zlib.DEFLATED, -15)
        raw = packer.compress(RUN * 50) + packer.flush()
        path = crafted(tmp_path, {"method": 8, "raw": raw[:-3]})
        with pytest.raises(PackageError):
            inspect_bundle(path)

    @pytest.mark.parametrize(
        "change", [{"lcrc": 0xDEADBEEF}, {"lsize": 3}, {"lcsize": 5}], ids=["crc", "size", "csize"]
    )
    def test_local_header_disagrees_with_the_directory(self, tmp_path, change):
        """R3"""
        with pytest.raises(PackageError):
            inspect_bundle(crafted(tmp_path, change))

    def test_unicode_path_extra_field_naming_another_file(self, tmp_path):
        """R4: Info-ZIP unzip and Python 3.12+ would call this member other.py."""
        extra = unicode_path_extra("run.py", "other.py")
        path = crafted(tmp_path, {"cextra": extra, "lextra": extra})
        with pytest.raises(PackageError, match="extra fields"):
            inspect_bundle(path)

    @pytest.mark.parametrize("where", ["cextra", "lextra"])
    def test_any_extra_field_is_refused(self, tmp_path, where):
        """Including a 60 KB unknown one in the directory only (memory) or a
        timestamp field: packages carry none."""
        pad = struct.pack("<HH", 0xCAFE, 60000) + bytes(60000)
        with pytest.raises(PackageError, match="extra fields|directory is larger"):
            inspect_bundle(crafted(tmp_path, {where: pad}))

    def test_a_small_timestamp_extra_in_the_directory_only(self, tmp_path):
        timestamp = struct.pack("<HHBL", 0x5455, 5, 1, 0)
        with pytest.raises(PackageError, match="extra fields"):
            inspect_bundle(crafted(tmp_path, {"cextra": timestamp}))

    def test_data_descriptor_with_matching_values(self, tmp_path):
        with pytest.raises(PackageError, match="data descriptor"):
            inspect_bundle(crafted(tmp_path, {"flags": 0x08}))

    @pytest.mark.parametrize("flag", [0x0020, 0x0010, 0x4000])
    def test_unused_flags(self, tmp_path, flag):
        with pytest.raises(PackageError, match="flags"):
            inspect_bundle(crafted(tmp_path, {"flags": flag}))

    def test_zip_version_beyond_2_0(self, tmp_path):
        with pytest.raises(PackageError, match="version"):
            inspect_bundle(crafted(tmp_path, {"version": 45}))

    def test_stored_member_with_two_sizes(self, tmp_path):
        with pytest.raises(PackageError):
            inspect_bundle(crafted(tmp_path, {"file_size": len(RUN) - 1, "lsize": len(RUN) - 1}))

    def test_directory_order_must_match_the_members(self, tmp_path):
        manifest = json.dumps(manifest_for({"run.py": RUN})).encode()
        data = bytearray(
            craft([{"name": MANIFEST_NAME, "data": manifest}, {"name": "run.py", "data": RUN}])
        )
        (first, _), (second, _) = central_entries(bytes(data))
        one, two = bytes(data[first:second]), bytes(data[second : len(data) - 22])
        swapped = data[:first] + two + one + data[len(data) - 22 :]
        (tmp_path / "s.zip").write_bytes(bytes(swapped))
        with pytest.raises(PackageError):
            inspect_bundle(tmp_path / "s.zip")

    def test_python_zipfile_reads_exactly_what_was_verified(self, bundle, source):
        info, path = bundle
        with zipfile.ZipFile(path) as archive:
            assert [item.filename for item in archive.infolist()] == [MANIFEST_NAME, *CLEAN]
            assert all(not item.extra and not item.flag_bits & 0x08 for item in archive.infolist())
            for entry in info.manifest["files"]:
                assert sha(archive.read(entry["path"])) == entry["sha256"]


class TestStage:
    def test_stage_keeps_exactly_the_verified_bytes(self, tmp_path, bundle):
        info, path = bundle
        (tmp_path / "staging").mkdir()
        staged = pk.stage_bundle(path, tmp_path / "staging", expected_sha256=info.sha256)
        assert staged.info == info
        assert sha(staged.path.read_bytes()) == info.sha256
        if sys.platform != "win32":
            assert stat.S_IMODE(staged.path.stat().st_mode) == 0o600
        path.write_bytes(b"replaced afterwards")
        assert sha(staged.path.read_bytes()) == info.sha256

    def test_stage_refusals_leave_nothing(self, tmp_path, bundle):
        _, path = bundle
        (tmp_path / "staging").mkdir()
        with pytest.raises(pk.DigestMismatch):
            pk.stage_bundle(path, tmp_path / "staging", expected_sha256="0" * 64)
        (tmp_path / "bad.zip").write_bytes(b"not a zip")
        with pytest.raises(PackageError):
            pk.stage_bundle(tmp_path / "bad.zip", tmp_path / "staging")
        assert os.listdir(tmp_path / "staging") == []


class TestExclusionGaps:
    @pytest.mark.parametrize("path", [".git", "vendor/lib/.git", "src/.GIT"])
    def test_git_pointer_files_are_refused(self, tmp_path, source, path):
        """PM1: a worktree's .git file holds an absolute private path."""
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("gitdir: /home/someone/private/repo/.git\n", encoding="utf-8")
        with pytest.raises(PackageError, match="may not be packaged"):
            build_bundle(source, tmp_path / "o.zip", dict(META), [*CLEAN, path])

    def test_git_pointer_files_are_not_suggested_without_git(self, tmp_path, source, monkeypatch):
        (source / ".git").write_text("gitdir: /home/someone/private/.git\n", encoding="utf-8")
        (source / "vendor").mkdir()
        (source / "vendor" / ".git").write_text("gitdir: /x\n", encoding="utf-8")
        empty = tmp_path / "no-tools"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        assert suggest_files(source) == CLEAN

    def test_git_errors_do_not_repeat_local_paths(self, tmp_path, source):
        """m2: with git installed, a broken .git pointer is an error whose
        message names no path; without git the pointer is just left out."""
        (source / ".git").write_text(
            "gitdir: /home/private-user/lab/main-repo/.git/worktrees/exp\n", encoding="utf-8"
        )
        try:
            suggested = suggest_files(source)
        except PackageError as error:
            assert "/home/" not in str(error) and str(tmp_path) not in str(error)
        else:
            assert ".git" not in suggested

    @pytest.mark.parametrize(
        "path",
        [
            "results/sub-josé/ses-001/trials.csv",
            "anywhere/SUB-Ab1/x.json",
            "custom_root/ｓｕｂ-01/x.csv",
            "logs/sub-01_ses-001_run-01_task-x_trials.csv",
        ],
    )
    def test_subject_data_is_refused_whatever_the_code(self, tmp_path, source, path):
        """PM2: subject codes are any isalnum() text."""
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("subject,rt\n", encoding="utf-8")
        with pytest.raises(PackageError, match="may not be packaged"):
            build_bundle(source, tmp_path / "o.zip", dict(META), [*CLEAN, path])
        assert path not in suggest_files(source)


class TestPortability:
    @pytest.mark.parametrize("pair", [("ı.py", "I.py"), ("ſ.py", "S.py")])
    def test_names_windows_compares_as_one(self, pair):
        """m5: NTFS upper-cases; dotless i and long s collide with I and S."""
        with pytest.raises(PackageError, match="case"):
            pk._check_path_set(["run.py", *pair])

    @pytest.mark.parametrize("path", ["abcdef~1.py", "src/PROGRA~1/x.py", "a~9"])
    def test_names_that_look_like_short_names(self, path):
        with pytest.raises(PackageError, match="short"):
            safe_relative(path)

    def test_package_paths_have_a_length_cap(self, tmp_path, source):
        long = "/".join(["folder"] * 30) + "/x.py"
        assert len(long) > pk.MAX_PACKAGE_PATH_CHARS
        assert safe_relative(long) == long  # still a portable path in general
        target = source / long
        target.parent.mkdir(parents=True)
        target.write_text("", encoding="utf-8")
        with pytest.raises(PackageError, match="at most"):
            build_bundle(source, tmp_path / "o.zip", dict(META), [*CLEAN, long])

    def test_only_name_surrogate_reparse_points_count_as_links(self):
        import types

        def node(tag: int, attributes: int = 0x400) -> os.stat_result:
            return types.SimpleNamespace(  # type: ignore[return-value]
                st_mode=stat.S_IFREG | 0o644, st_file_attributes=attributes, st_reparse_tag=tag
            )

        assert pk._is_link(node(0xA000000C))  # symbolic link
        assert pk._is_link(node(0xA0000003))  # junction (mount point)
        assert not pk._is_link(node(0x9000001A))  # cloud-file placeholder
        assert not pk._is_link(node(0, attributes=0))


class TestLimits:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_archive_bytes": pk.DEFAULT_MAX_ARCHIVE_BYTES + 1},
            {"max_expanded_bytes": pk.DEFAULT_MAX_EXPANDED_BYTES + 1},
            {"max_files": pk.DEFAULT_MAX_FILES + 1},
        ],
    )
    def test_limits_cannot_exceed_what_every_rig_installs(self, bundle, kwargs):
        """m3: a hub may accept less than the format allows, never more."""
        _, path = bundle
        with pytest.raises(ValueError):
            inspect_bundle(path, **kwargs)
        with pytest.raises(ValueError):
            pk.install_bundle(path, path.parent / "never", **kwargs)
        assert not (path.parent / "never").exists()

    def test_install_honours_lower_limits(self, tmp_path, bundle):
        _, path = bundle
        with pytest.raises(PackageError, match="members"):
            extract_bundle(path, tmp_path / "demo", max_files=2)
        assert not (tmp_path / "demo").exists()


@POSIX_ONLY
class TestFifo:
    def test_a_fifo_is_refused_without_blocking(self, tmp_path):
        """m1"""
        os.mkfifo(tmp_path / "pkg.zip")
        code = (
            "import sys\nfrom pathlib import Path\nfrom alhazen.hub import packages as P\n"
            "for call in (lambda: P.inspect_bundle(Path(sys.argv[1])),\n"
            "             lambda: P.extract_bundle(Path(sys.argv[1]), Path(sys.argv[2]))):\n"
            "    try:\n        call()\n"
            "    except P.PackageError as e:\n        print('refused', e)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path / "pkg.zip"), str(tmp_path / "dest")],
            env=child_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.stdout.count("refused") == 2, result.stderr
        assert "not a regular file" in result.stdout
        assert sorted(os.listdir(tmp_path)) == ["pkg.zip"]


def child_env() -> dict[str, str]:
    """The environment for a child Python that imports this alhazen."""
    root = str(Path(pk.__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


class TestInstallOutcome:
    def test_expected_digest_is_checked_before_anything_is_installed(self, tmp_path, bundle):
        """PM4"""
        info, path = bundle
        with pytest.raises(pk.DigestMismatch):
            extract_bundle(path, tmp_path / "demo", expected_sha256="f" * 64)
        assert sorted(os.listdir(tmp_path)) == ["demo.zip", "experiment"]
        assert extract_bundle(path, tmp_path / "demo", expected_sha256=info.sha256) == info

    @pytest.mark.parametrize("value", ["ABC", "F" * 64, 5])
    def test_expected_digest_must_be_a_digest(self, tmp_path, bundle, value):
        _, path = bundle
        with pytest.raises(ValueError):
            extract_bundle(path, tmp_path / "demo", expected_sha256=value)

    def test_install_reports_durability(self, tmp_path, bundle):
        info, path = bundle
        result = pk.install_bundle(path, tmp_path / "demo")
        assert result.info == info and result.destination == tmp_path / "demo"
        assert result.durable == (sys.platform != "win32")
        assert sorted(os.listdir(tmp_path)) == ["demo", "demo.zip", "experiment"]

    def test_unconfirmed_sync_after_the_commit_is_not_a_failure(
        self, tmp_path, bundle, monkeypatch
    ):
        """PM3/R7: the rename is the commit. A folder sync refused (or failing)
        afterwards gives an installed, not-durable result, never 'failed'."""
        info, path = bundle
        real = pk._fsync_directory

        def refusing(folder):
            if folder == tmp_path:
                raise OSError(5, "Input/output error")
            return real(folder)

        monkeypatch.setattr(pk, "_fsync_directory", refusing)
        with pytest.raises(PackageError):  # before the commit: still a clean failure
            extract_bundle(path, tmp_path / "early")
        assert sorted(os.listdir(tmp_path)) == ["demo.zip", "experiment"]

        calls = {"n": 0}

        def refusing_after_commit(folder):
            if folder == tmp_path:
                calls["n"] += 1
                if calls["n"] > 1:
                    raise OSError(22, "Invalid argument")
            return real(folder)

        monkeypatch.setattr(pk, "_fsync_directory", refusing_after_commit)
        with pytest.raises(pk.InstallNotDurable) as caught:
            extract_bundle(path, tmp_path / "demo")
        result = caught.value.result
        assert result.info == info and not result.durable
        assert "final rename" in result.durability_note
        assert sorted(p.name for p in (tmp_path / "demo").iterdir())
        assert sorted(os.listdir(tmp_path)) == ["demo", "demo.zip", "experiment"]

    def test_unsupported_directory_sync_is_reported_not_raised(self, tmp_path, bundle, monkeypatch):
        _, path = bundle
        monkeypatch.setattr(pk, "_fsync_directory", lambda folder: False)
        result = pk.install_bundle(path, tmp_path / "demo")
        assert not result.durable and "not confirmed" in result.durability_note
        assert (tmp_path / "demo" / "run.py").is_file()

    def test_cleanup_failure_keeps_the_original_error(self, tmp_path, monkeypatch):
        """m6: the refusal is what the caller sees; the failed cleanup is a
        note, and the record stays so recover_install can finish."""
        members = good_members()
        path = raw_bundle(tmp_path / "b.zip", {**members, "src/m.py": b"evil"}, declare=members)
        real = pk._remove_tree_or_file

        def stuck(leftover):
            if leftover.name.endswith(".package"):
                raise OSError(16, "Device or resource busy")
            return real(leftover)

        monkeypatch.setattr(pk, "_remove_tree_or_file", stuck)
        with pytest.raises(PackageError, match="does not match") as caught:
            extract_bundle(path, tmp_path / "demo")
        notes = getattr(caught.value, "__notes__", None) or caught.value.cleanup_notes
        assert any("could not remove" in note for note in notes)
        assert any(name.endswith(".alhazen-install") for name in os.listdir(tmp_path))
        monkeypatch.setattr(pk, "_remove_tree_or_file", real)
        recovered = pk.recover_install(tmp_path / "demo")
        assert recovered.record_found and recovered.destination_state == "absent"
        assert sorted(os.listdir(tmp_path)) == ["b.zip"]


KILL_AT = {
    "record": "pk._Durability.directory",
    "copy": "pk._verified_copy",
    "extract": "pk._extract_verified",
    "commit": "pk._replace_onto_claim",
}


def die_during_install(bundle: Path, destination: Path, phase: str) -> int:
    """Run an install in a child that SIGKILLs itself right after ``phase``."""
    code = (
        "import os, signal, sys\nfrom pathlib import Path\n"
        "from alhazen.hub import packages as pk\n"
        f"target = {KILL_AT[phase]!r}\n"
        "owner_name, attr = target.rsplit('.', 1)\n"
        "owner = eval(owner_name)\nreal = getattr(owner, attr)\n"
        "def wrapped(*args, **kwargs):\n"
        "    result = real(*args, **kwargs)\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
        "    return result\n"
        "setattr(owner, attr, wrapped)\n"
        "pk.install_bundle(Path(sys.argv[1]), Path(sys.argv[2]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(bundle), str(destination)],
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode


@POSIX_ONLY
class TestCrashRecovery:
    """PM3/R8: a crash at any phase leaves leftovers that recover_install
    removes (and only those), after which the install can be repeated."""

    @pytest.mark.parametrize("phase", ["record", "copy", "extract"])
    def test_crash_before_the_commit(self, tmp_path, bundle, phase):
        info, path = bundle
        destination = tmp_path / "demo"
        assert die_during_install(path, destination, phase) == -9
        leftovers = sorted(set(os.listdir(tmp_path)) - {"demo.zip", "experiment"})
        assert ".demo.alhazen-install" in leftovers
        with pytest.raises(pk.InstallInterrupted):
            pk.install_bundle(path, destination)
        recovered = pk.recover_install(destination)
        assert recovered.record_found
        assert recovered.destination_state in ("absent", "claim-removed")
        assert sorted(os.listdir(tmp_path)) == ["demo.zip", "experiment"]
        assert set(recovered.removed) == set(leftovers)
        assert pk.install_bundle(path, destination).info == info
        assert pk.recover_install(destination).record_found is False

    def test_crash_right_after_the_commit(self, tmp_path, bundle):
        info, path = bundle
        destination = tmp_path / "demo"
        assert die_during_install(path, destination, "commit") == -9
        recovered = pk.recover_install(destination)
        assert recovered.destination_state == "installed"
        assert sorted(os.listdir(tmp_path)) == ["demo", "demo.zip", "experiment"]
        for entry in info.manifest["files"]:
            assert sha((destination / entry["path"]).read_bytes()) == entry["sha256"]

    def test_a_running_install_cannot_be_recovered_or_joined(self, tmp_path, bundle):
        _, path = bundle
        destination = tmp_path / "demo"
        gate = tmp_path / "gate"
        code = (
            "import sys, time\nfrom pathlib import Path\nfrom alhazen.hub import packages as pk\n"
            "real = pk._extract_verified\ngate = Path(sys.argv[3])\n"
            "def slow(*args):\n    real(*args)\n    gate.write_text('in', encoding='utf-8')\n"
            "    deadline = time.time() + 30\n"
            "    while gate.exists() and time.time() < deadline:\n        time.sleep(0.02)\n"
            "pk._extract_verified = slow\n"
            "print(pk.install_bundle(Path(sys.argv[1]), Path(sys.argv[2])).durable)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(path), str(destination), str(gate)],
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 30
            while not gate.exists() and time.time() < deadline:
                time.sleep(0.02)
            assert gate.exists(), "the child install never reached its staging step"
            with pytest.raises(pk.InstallInProgress):
                pk.recover_install(destination)
            with pytest.raises(pk.InstallInterrupted):
                pk.install_bundle(path, destination)
            gate.unlink()
            out, err = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
        assert child.returncode == 0, err
        assert (destination / "run.py").is_file()
        assert sorted(os.listdir(tmp_path)) == ["demo", "demo.zip", "experiment"]

    def test_recovery_never_deletes_what_it_did_not_create(self, tmp_path):
        destination = tmp_path / "demo"
        destination.mkdir()
        (destination / "mine.txt").write_text("keep", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        nonce = "a" * 32
        record = tmp_path / ".demo.alhazen-install"
        record.write_text(
            json.dumps({"format": 1, "destination": "demo", "nonce": nonce, "pid": 1}),
            encoding="utf-8",
        )
        os.symlink(outside, tmp_path / f".demo.{nonce}.staging")
        with pytest.raises(PackageError, match="left in place"):
            pk.recover_install(destination)
        assert (outside / "keep.txt").exists() and (destination / "mine.txt").exists()
        os.unlink(tmp_path / f".demo.{nonce}.staging")
        recovered = pk.recover_install(destination)
        assert recovered.destination_state == "installed"
        assert (destination / "mine.txt").exists() and not record.exists()

    def test_recovery_without_a_record_touches_nothing(self, tmp_path):
        assert pk.recover_install(tmp_path / "demo").destination_state == "absent"
        (tmp_path / "demo").mkdir()
        result = pk.recover_install(tmp_path / "demo")
        assert (result.record_found, result.destination_state) == (False, "kept")
        assert (tmp_path / "demo").is_dir()

    @pytest.mark.parametrize(
        "body",
        [
            "{not json",
            json.dumps({"format": 1, "destination": "other", "nonce": "a" * 32}),
            json.dumps({"format": 1, "destination": "demo", "nonce": "../../x"}),
        ],
    )
    def test_a_damaged_or_foreign_record_is_not_acted_on(self, tmp_path, body):
        (tmp_path / "demo").mkdir()
        (tmp_path / ".demo.alhazen-install").write_text(body, encoding="utf-8")
        with pytest.raises(PackageError):
            pk.recover_install(tmp_path / "demo")
        assert (tmp_path / "demo").is_dir()
        assert (tmp_path / ".demo.alhazen-install").exists()
