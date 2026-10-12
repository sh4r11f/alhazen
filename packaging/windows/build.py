"""Build the Alhazen app's Windows installer.

    python packaging/windows/build.py --wheel dist/alhazen_vision-2.15.0-py3-none-any.whl

What it does, in order:

1. **Fetch** the two programs the app brings with it, each from its
   publisher's release page and each checked against a SHA-256 written in this
   file (``PINNED``): ``uv``, which builds Python environments, and MinGit,
   the distribution of Git for Windows made for bundling, which uv needs when
   an experiment takes a package from a git repository.
2. **Stage** the folder the installer copies to the computer: those tools, the
   alhazen wheel, the icon and the licences.
3. **Compile** ``alhazen.iss`` with Inno Setup into ``AlhazenSetup-<version>.exe``.

Why the checksums are in this file and not looked up: a download is only
trusted because it matches a hash someone reviewed. To move to a newer uv or
Git, change the version, the address and the hash together, taking the hash
from the publisher (uv publishes ``<asset>.sha256``; Git for Windows lists
them in the release notes), and let CI build and test the result.

The Python the app runs on and alhazen's own dependencies are NOT in the
installer: uv downloads them on the computer during installation. That keeps
the installer small (about 40 MB) at the price of needing a connection then.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]

# What the installer bundles. ``members`` limits what is taken from an
# archive (None takes everything); ``into`` is the folder under ``tools``.
PINNED = {
    "uv": {
        "version": "0.12.19",
        "url": "https://github.com/astral-sh/uv/releases/download/0.12.19/"
        "uv-x86_64-pc-windows-msvc.zip",
        "sha256": "6dbb02d79e419522f1c500f0adb1cddcff0cda7d59b0d66ea7f5e3b4a1b2f5f0",
        "source": "https://github.com/astral-sh/uv/tree/0.12.19",
        # The archive also holds uvx.exe and uvw.exe, which the app never runs.
        "members": ["uv.exe"],
        "into": "",
    },
    "git": {
        "version": "2.56.0.2",
        "url": "https://github.com/git-for-windows/git/releases/download/"
        "v2.56.0.windows.2/MinGit-2.56.0.2-64-bit.zip",
        "sha256": "da35e72aa21c005a5a0d298cfbae110bc1609a815730ea0dde84b01a1b3cd3be",
        # Where this exact version's source is, for the licence notice.
        "source": "https://github.com/git-for-windows/git/releases/tag/v2.56.0.windows.2",
        "members": None,
        "into": "git",
    },
}
# Where Inno Setup's compiler is when it is not on PATH.
DEFAULT_ISCC = Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")


class BuildError(RuntimeError):
    """The installer cannot be built; the message says what to do."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def fetch(name: str, cache: Path, pinned: dict | None = None) -> Path:
    """The archive of the pinned tool ``name``, downloaded into ``cache``
    unless a copy with the right hash is already there. A download whose
    hash differs is removed and refused: it is not what was reviewed."""
    spec = (pinned or PINNED)[name]
    target = cache / spec["url"].rsplit("/", 1)[1]
    if target.is_file() and sha256_of(target) == spec["sha256"]:
        return target
    cache.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    with urllib.request.urlopen(spec["url"], timeout=120) as response, partial.open("wb") as out:
        shutil.copyfileobj(response, out)
    found = sha256_of(partial)
    if found != spec["sha256"]:
        partial.unlink()
        raise BuildError(
            f"{name} {spec['version']}: the download from {spec['url']} has SHA-256 {found}, "
            f"not the pinned {spec['sha256']}. Nothing was kept. If the publisher replaced "
            "the file, review the new one and update PINNED in packaging/windows/build.py."
        )
    partial.replace(target)
    return target


def version_of(wheel: Path) -> str:
    """The version in a wheel's file name (``alhazen_vision-2.15.0-py3-...``)."""
    parts = wheel.name.split("-")
    if len(parts) < 3 or parts[0] != "alhazen_vision" or wheel.suffix != ".whl":
        raise BuildError(f"{wheel.name} is not an alhazen-vision wheel")
    return parts[1]


def stage(wheel: Path, out: Path, archives: dict[str, Path], pinned: dict | None = None) -> Path:
    """Lay out ``out/stage``: exactly what the installer copies to
    ``%LOCALAPPDATA%\\Alhazen``. Rebuilt from scratch each time, so a file
    from an earlier build can never ride along."""
    specs = pinned or PINNED
    root = out / "stage"
    if root.exists():
        shutil.rmtree(root)
    tools = root / "tools"
    for name, archive in archives.items():
        spec = specs[name]
        destination = tools / spec["into"] if spec["into"] else tools
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            # The archive was just checked against its reviewed hash, so its
            # member names are the publisher's own.
            bundle.extractall(destination, members=spec["members"])
    (root / "wheels").mkdir(parents=True)
    shutil.copy2(wheel, root / "wheels" / wheel.name)
    shutil.copy2(HERE / "alhazen.ico", root / "alhazen.ico")
    shutil.copy2(REPOSITORY / "LICENSE", root / "LICENSE.txt")
    notices = (HERE / "THIRD-PARTY.txt").read_text(encoding="utf-8")
    # The notice names the exact versions bundled and where their source is.
    for name, spec in specs.items():
        notices = notices.replace("{" + name + "_version}", spec["version"])
        notices = notices.replace("{" + name + "_source}", spec["source"])
    (root / "THIRD-PARTY.txt").write_text(notices, encoding="utf-8")
    # What the installed app looks for (alhazen.hub.environments.find_tools).
    for needed in (tools / "uv.exe", tools / "git" / "cmd" / "git.exe"):
        if not needed.is_file():
            raise BuildError(f"The staged tools lack {needed.relative_to(root)}")
    return root


def find_iscc(given: str | None) -> Path:
    for candidate in (given, shutil.which("ISCC"), str(DEFAULT_ISCC)):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise BuildError(
        "Inno Setup's compiler (ISCC.exe) was not found. Install Inno Setup 6 "
        "(https://jrsoftware.org/isinfo.php), or pass --iscc PATH, or use --stage-only."
    )


def compile_installer(iscc: Path, staged: Path, wheel: Path, out: Path) -> Path:
    """Run Inno Setup on ``alhazen.iss``; the installer it wrote."""
    version = version_of(wheel)
    command = [
        str(iscc),
        f"/DAppVersion={version}",
        f"/DStage={staged}",
        f"/DWheel={wheel.name}",
        f"/O{out}",
        str(HERE / "alhazen.iss"),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise BuildError(
            f"Inno Setup failed (exit code {result.returncode}):\n"
            + (result.stdout + result.stderr)[-3000:]
        )
    installer = out / f"AlhazenSetup-{version}.exe"
    if not installer.is_file():
        raise BuildError(f"Inno Setup reported success but wrote no {installer.name}")
    return installer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Alhazen app's Windows installer.")
    parser.add_argument("--wheel", required=True, help="the alhazen-vision wheel to install")
    parser.add_argument("--out", default="build/windows", help="output folder")
    parser.add_argument("--iscc", default=None, help="path of Inno Setup's ISCC.exe")
    parser.add_argument(
        "--stage-only", action="store_true", help="fetch and lay out the files; do not compile"
    )
    args = parser.parse_args(argv)
    wheel = Path(args.wheel).resolve()
    out = Path(args.out).resolve()
    try:
        if not wheel.is_file():
            raise BuildError(f"No wheel at {wheel}; build one first (python -m build --wheel)")
        version_of(wheel)
        archives = {name: fetch(name, out / "cache") for name in PINNED}
        staged = stage(wheel, out, archives)
        print(f"Staged {staged}")
        if args.stage_only:
            return 0
        installer = compile_installer(find_iscc(args.iscc), staged, wheel, out)
    except BuildError as exc:
        print(f"CANNOT BUILD THE INSTALLER: {exc}", file=sys.stderr)
        return 1
    megabytes = installer.stat().st_size / 1e6
    print(f"Built {installer} ({megabytes:.1f} MB, sha256 {sha256_of(installer)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
