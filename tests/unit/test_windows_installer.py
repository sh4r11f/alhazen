"""packaging/windows/build.py: what goes into the Windows installer.

The compile step (Inno Setup) and a real install run only in CI
(.github/workflows/windows-app.yml). Here: the downloads are accepted only on
their reviewed hash, the staged folder is exactly what the installed app
looks for, and the installer script agrees with the app about that layout."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import zipfile
from pathlib import Path

import pytest

from alhazen.hub.environments import app_tools_folder, find_tools

try:  # tomllib is stdlib from 3.11; tomli is the dev-extra backport for 3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

REPOSITORY = Path(__file__).resolve().parents[2]
PACKAGING = REPOSITORY / "packaging" / "windows"


@pytest.fixture(scope="module")
def build():
    """packaging/windows/build.py, loaded from its file (it is a script
    beside the installer's sources, not part of the alhazen package)."""
    spec = importlib.util.spec_from_file_location("windows_build", PACKAGING / "build.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["windows_build"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["windows_build"]


def archive(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return path


def pinned_for(tmp_path, *, uv_hash=None, git_hash=None):
    """Two small stand-in archives, and a PINNED table that names them by
    file address and by their real hashes (or the given wrong ones)."""
    source = tmp_path / "published"
    source.mkdir(exist_ok=True)
    uv = archive(source / "uv.zip", {"uv.exe": b"uv", "uvx.exe": b"uvx", "uvw.exe": b"uvw"})
    git = archive(
        source / "MinGit.zip",
        {"cmd/git.exe": b"git", "LICENSE.txt": b"GPL", "usr/bin/sh.exe": b"sh"},
    )

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    return {
        "uv": {
            "version": "9.9.9",
            "url": uv.as_uri(),
            "sha256": uv_hash or sha(uv),
            "source": "https://example.org/uv/9.9.9",
            "members": ["uv.exe"],
            "into": "",
        },
        "git": {
            "version": "8.8.8",
            "url": git.as_uri(),
            "sha256": git_hash or sha(git),
            "source": "https://example.org/git/8.8.8",
            "members": None,
            "into": "git",
        },
    }


class TestFetch:
    def test_a_download_with_the_pinned_hash_is_kept_and_reused(self, build, tmp_path):
        pinned = pinned_for(tmp_path)
        cache = tmp_path / "cache"
        first = build.fetch("uv", cache, pinned)
        assert first == cache / "uv.zip" and first.is_file()
        # Reused without the source: nothing is downloaded a second time.
        (tmp_path / "published" / "uv.zip").unlink()
        assert build.fetch("uv", cache, pinned) == first

    def test_a_download_with_another_hash_is_refused_and_not_kept(self, build, tmp_path):
        pinned = pinned_for(tmp_path, uv_hash="0" * 64)
        cache = tmp_path / "cache"
        with pytest.raises(build.BuildError) as refusal:
            build.fetch("uv", cache, pinned)
        message = str(refusal.value)
        assert "uv 9.9.9" in message and "0" * 64 in message and "Nothing was kept" in message
        assert list(cache.iterdir()) == []

    def test_a_cached_file_that_no_longer_matches_is_downloaded_again(self, build, tmp_path):
        pinned = pinned_for(tmp_path)
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "uv.zip").write_bytes(b"tampered")
        fetched = build.fetch("uv", cache, pinned)
        assert hashlib.sha256(fetched.read_bytes()).hexdigest() == pinned["uv"]["sha256"]


class TestStage:
    def staged(self, build, tmp_path, pinned=None):
        pinned = pinned or pinned_for(tmp_path)
        cache = tmp_path / "cache"
        wheel = tmp_path / "alhazen_vision-2.15.0-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        archives = {name: build.fetch(name, cache, pinned) for name in pinned}
        return build.stage(wheel, tmp_path / "out", archives, pinned)

    def test_the_folder_is_what_the_installed_app_looks_for(self, build, tmp_path):
        root = self.staged(build, tmp_path)
        files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        assert files == [
            "LICENSE.txt",
            "THIRD-PARTY.txt",
            "alhazen.ico",
            "tools/git/LICENSE.txt",
            "tools/git/cmd/git.exe",
            "tools/git/usr/bin/sh.exe",
            # uv alone: uvx.exe and uvw.exe are not taken from the archive.
            "tools/uv.exe",
            "wheels/alhazen_vision-2.15.0-py3-none-any.whl",
        ]
        if sys.platform == "win32":
            # The app's environment is installed beside `tools`, as `app`:
            # from there the app finds exactly these two programs.
            assert app_tools_folder(str(root / "app")) == root / "tools"
            tools = find_tools(prefix=str(root / "app"), which=lambda name: None)
            assert tools.uv == root / "tools" / "uv.exe"
            assert tools.git == root / "tools" / "git" / "cmd" / "git.exe"

    def test_the_notice_names_the_bundled_versions_and_their_source(self, build, tmp_path):
        notice = (self.staged(build, tmp_path) / "THIRD-PARTY.txt").read_text(encoding="utf-8")
        assert "uv 9.9.9" in notice and "https://example.org/uv/9.9.9" in notice
        assert "(MinGit) 8.8.8" in notice and "https://example.org/git/8.8.8" in notice
        assert "{" not in notice  # every placeholder was filled

    def test_a_file_from_an_earlier_build_does_not_ride_along(self, build, tmp_path):
        root = self.staged(build, tmp_path)
        (root / "tools" / "left-over.exe").write_bytes(b"old")
        assert not (self.staged(build, tmp_path) / "tools" / "left-over.exe").exists()

    def test_an_archive_without_the_program_is_refused(self, build, tmp_path):
        pinned = pinned_for(tmp_path)
        empty = archive(tmp_path / "published" / "MinGit.zip", {"README": b"no git here"})
        pinned["git"]["sha256"] = hashlib.sha256(empty.read_bytes()).hexdigest()
        with pytest.raises(build.BuildError, match="git"):
            self.staged(build, tmp_path, pinned)


class TestTheScript:
    def test_the_version_comes_from_the_wheels_name(self, build, tmp_path):
        assert build.version_of(Path("alhazen_vision-2.15.0-py3-none-any.whl")) == "2.15.0"
        for wrong in ("other_pkg-1.0-py3-none-any.whl", "alhazen_vision-2.15.0.tar.gz"):
            with pytest.raises(build.BuildError):
                build.version_of(Path(wrong))

    def test_a_missing_compiler_says_how_to_get_one(self, build, monkeypatch, tmp_path):
        monkeypatch.setattr(build, "DEFAULT_ISCC", tmp_path / "no-such-ISCC.exe")
        monkeypatch.setattr(build.shutil, "which", lambda name: None)
        with pytest.raises(build.BuildError) as refusal:
            build.find_iscc(None)
        assert "Inno Setup" in str(refusal.value) and "--stage-only" in str(refusal.value)

    def test_a_missing_wheel_is_an_error_not_a_traceback(self, build, tmp_path, capsys):
        code = build.main(["--wheel", str(tmp_path / "absent.whl"), "--out", str(tmp_path / "o")])
        assert code == 1
        assert "CANNOT BUILD THE INSTALLER: No wheel at" in capsys.readouterr().err

    def test_every_pinned_download_is_https_from_its_publisher_with_a_full_hash(self, build):
        hosts = {
            "uv": "https://github.com/astral-sh/uv/releases/download/",
            "git": "https://github.com/git-for-windows/git/releases/download/",
        }
        assert set(build.PINNED) == set(hosts)
        for name, spec in build.PINNED.items():
            assert spec["url"].startswith(hosts[name]), name
            assert re.fullmatch(r"[0-9a-f]{64}", spec["sha256"]), name
            # The version in the table is the one in the address.
            assert spec["version"].rsplit(".", 1)[0] in spec["url"], name


class TestTheInstallerScript:
    """alhazen.iss cannot be compiled here; what can be held is that it
    agrees with the code about the layout and the names it relies on."""

    SCRIPT = (PACKAGING / "alhazen.iss").read_text(encoding="utf-8")

    def test_it_builds_the_environment_beside_the_tools_folder(self):
        # find_tools looks for <environment>/../tools: `app` and `tools` must
        # be siblings in the install folder.
        assert r"ExpandConstant('{app}\app')" in self.SCRIPT
        assert r"{app}\tools\uv.exe" in self.SCRIPT
        assert app_tools_folder("C:/Alhazen/app") == Path("C:/Alhazen/tools")

    def test_the_shortcut_runs_the_launcher_the_package_installs(self):
        project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
        assert "alhazen-app" in project["project"]["gui-scripts"]
        assert self.SCRIPT.count(r"{app}\app\Scripts\alhazen-app.exe") >= 2

    def test_it_installs_for_one_user_without_administrator_rights(self):
        assert "PrivilegesRequired=lowest" in self.SCRIPT
        assert r"DefaultDirName={localappdata}\Alhazen" in self.SCRIPT

    def test_its_identity_never_changes(self):
        # A different AppId would install a second copy beside the first.
        assert "AppId={{597AD17D-7C55-4184-847B-9E905C398D83}" in self.SCRIPT

    def test_the_build_passes_every_definition_the_script_requires(self, build):
        required = set(re.findall(r"#ifndef (\w+)", self.SCRIPT))
        source = (PACKAGING / "build.py").read_text(encoding="utf-8")
        assert required == {"AppVersion", "Stage", "Wheel"}
        for name in required:
            assert f"/D{name}=" in source
