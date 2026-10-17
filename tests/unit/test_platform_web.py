"""The compiled web app shipped in the wheel (src/alhazen/experiment_platform/web/).

platform-web/build.mjs compiles it from platform-web/src. These tests pin what a rig or a
server relies on without Node: every file the page needs is packaged, nothing is fetched
from another origin, the font is the one the dashboard vendors, and the Mana adapter is not
part of the standalone bundle. When Node and the build dependencies are present, the
committed files must also equal a fresh build (the same gate as `build.mjs --check`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest

WEB = resources.files("alhazen.experiment_platform") / "web"
REPO = Path(__file__).resolve().parents[2]
FILES = [
    "index.html",
    "platform.js",
    "platform.css",
    "assets.json",
    "fonts/Manrope-latin.woff2",
    "fonts/OFL.txt",
    "THIRD_PARTY_LICENSES.txt",
]
FOREIGN = re.compile(r"fonts\.googleapis|fonts\.gstatic|esm\.sh|unpkg\.com|jsdelivr|cdnjs", re.I)


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_every_file_the_page_needs_is_packaged():
    missing = [name for name in FILES if not (WEB / name).is_file()]
    assert missing == []


def test_the_page_loads_only_its_own_files():
    html = read("index.html")
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert sorted(refs) == ["assets/platform.css", "assets/platform.js"]
    assert "<style" not in html and "<script>" not in html  # nothing inline for the CSP to refuse


def test_nothing_is_fetched_from_a_cdn():
    for name in ("index.html", "platform.js", "platform.css"):
        assert FOREIGN.search(read(name)) is None, name
    css = read("platform.css")
    assert re.findall(r"url\(([^)]*)\)", css) == ["'fonts/Manrope-latin.woff2'"]


def test_the_font_is_the_one_the_dashboard_vendors():
    fonts = resources.files("alhazen.cli") / "assets" / "fonts"
    for name in ("Manrope-latin.woff2", "OFL.txt"):
        assert (WEB / "fonts" / name).read_bytes() == (fonts / name).read_bytes()


def test_the_standalone_bundle_does_not_contain_the_mana_adapter():
    js = read("platform.js")
    markers = (
        "mana.ask",
        "mana.mintChatId",
        "notification-navigate",
        "alhazen-experiment-platform-mine",
    )
    for marker in markers:
        assert marker not in js, marker


def test_assets_are_the_two_drawings():
    assets = json.loads(read("assets.json"))
    assert sorted(assets) == ["hero", "penrose"]
    assert all(value.lstrip().startswith("<svg") for value in assets.values())


def test_third_party_notices_name_the_bundled_react():
    notices = read("THIRD_PARTY_LICENSES.txt")
    assert "react 18.3.1 (MIT)" in notices
    assert "react-dom 18.3.1 (MIT)" in notices
    assert "SIL Open Font License" in notices


def _node_modules() -> Path | None:
    configured = os.environ.get("ALHAZEN_PLATFORM_NODE_MODULES")
    candidate = Path(configured) if configured else REPO / "platform-web" / "node_modules"
    return candidate if (candidate / "esbuild").is_dir() else None


@pytest.mark.skipif(
    shutil.which("node") is None or _node_modules() is None,
    reason="needs Node and the platform-web build dependencies",
)
def test_committed_assets_equal_a_fresh_build():
    env = dict(os.environ, ALHAZEN_PLATFORM_NODE_MODULES=str(_node_modules()))
    result = subprocess.run(
        ["node", str(REPO / "platform-web" / "build.mjs"), "--check"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
