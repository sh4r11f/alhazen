#!/usr/bin/env python3
"""The release gate: the git tag, pyproject.toml and CHANGELOG.md must agree.

A published version number is spent forever — PyPI refuses a reupload, and an
experiment that pinned `alhazen==1.1.0` gets whatever was uploaded under that
name for the rest of time. So the number is checked before anything is built.
There are three places it appears, and this script fails loudly on any
disagreement between them:

    pyproject.toml   version = "1.1.0"      the declared source of truth
    CHANGELOG.md     ## 1.1.0 - 2026-09-14  what a reader is told it is
    git tag          v1.1.0                 what CI builds and publishes from

Two modes, because there are two questions:

- **No `--tag`** — do pyproject and the changelog agree right now? True during
  development and after a release alike, so the test suite runs it on every
  commit.
- **`--tag v1.1.0`** — the release-day question. Adds: the tag names that same
  version, and nothing is still sitting under `Unreleased` that the release
  notes would silently omit.

Every check runs even after one fails, so you get all the problems at once.
Run it before tagging:

    python scripts/release_check.py --tag v1.1.0
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:  # tomllib is stdlib from 3.11; tomli is the dev-extra backport for 3.10.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[1]

# MAJOR.MINOR.PATCH with an optional prerelease (1.1.0-rc1). No build metadata:
# PyPI normalises it away, and a number that changes shape between the tag and
# the index is exactly the ambiguity this script exists to prevent.
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")

# `## 1.1.0 - 2026-09-14`. The separator may be a hyphen or an em dash, so the
# format is not a trap for anyone typing it by hand. The date is required: an
# entry without one cannot answer "which of these two installs is older?".
RELEASE_RE = re.compile(r"^##\s+(\S+)\s+[-–—]\s+(\d{4}-\d{2}-\d{2})\s*$")
UNRELEASED_RE = re.compile(r"^##\s+unreleased\s*$", re.IGNORECASE)
HEADING_RE = re.compile(r"^##\s+")


def version_key(text: str) -> tuple[int, int, int, int, str]:
    """A sortable key for a version string, or raise ValueError.

    The fourth field ranks a real release (1) above a prerelease of the same
    numbers (0), which is what semver says and what pip does: 1.1.0-rc1 comes
    before 1.1.0.
    """
    match = SEMVER_RE.match(text)
    if match is None:
        raise ValueError(f"{text!r} is not MAJOR.MINOR.PATCH[-prerelease]")
    major, minor, patch, prerelease = match.groups()
    return (int(major), int(minor), int(patch), 0 if prerelease else 1, prerelease or "")


def declared_version(root: Path) -> str:
    """The version in pyproject.toml's `[project]`, this repo's source of truth."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def read_changelog(root: Path) -> tuple[list[tuple[int, str, str]], int | None, bool]:
    """Parse CHANGELOG.md into what the checks below need.

    Returns the release sections as `(line number, version, date)` in file
    order, the line number of the `Unreleased` heading if there is one, and
    whether that section holds anything but blank lines — which is what tells
    an empty placeholder apart from changes a release would fail to document.
    """
    lines = (root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    headings = [n for n, line in enumerate(lines) if HEADING_RE.match(line)]

    releases: list[tuple[int, str, str]] = []
    unreleased_line: int | None = None
    unreleased_has_content = False

    for position, start in enumerate(headings):
        # A section runs from its heading to the next one, or to end of file.
        end = headings[position + 1] if position + 1 < len(headings) else len(lines)
        line = lines[start]
        if UNRELEASED_RE.match(line):
            unreleased_line = start + 1
            unreleased_has_content = any(body.strip() for body in lines[start + 1 : end])
        elif match := RELEASE_RE.match(line):
            releases.append((start + 1, match[1], match[2]))
        else:
            # Neither form. Recorded with an empty date so the caller reports
            # it instead of silently skipping a section it could not read.
            releases.append((start + 1, line.strip(), ""))

    return releases, unreleased_line, unreleased_has_content


def check(tag: str | None, root: Path = REPO_ROOT) -> list[str]:
    """Every problem found, as human-readable lines. An empty list means green."""
    problems: list[str] = []

    version = declared_version(root)
    try:
        version_key(version)
    except ValueError as error:
        # Nothing below is meaningful without a version to compare against.
        return [f"pyproject.toml: version {error}"]

    releases, unreleased_line, unreleased_has_content = read_changelog(root)

    # Report unreadable headings rather than skipping them: a skipped heading
    # silently weakens the "newest first" check below.
    readable: list[tuple[int, str, str]] = []
    for line_number, name, date in releases:
        if not date or not SEMVER_RE.match(name):
            problems.append(
                f"CHANGELOG.md:{line_number}: heading {name!r} is neither '## Unreleased' "
                f"nor '## <version> - <YYYY-MM-DD>'"
            )
        else:
            readable.append((line_number, name, date))

    # The declared version must be the newest release the changelog documents.
    if not readable:
        problems.append("CHANGELOG.md: no dated release section found")
    elif readable[0][1] != version:
        problems.append(
            f"CHANGELOG.md:{readable[0][0]}: newest release section is {readable[0][1]!r}, "
            f"but pyproject.toml declares {version!r} — bump both in the same commit"
        )

    # Newest first, strictly decreasing. Catches a duplicated entry, and a new
    # section pasted in below an older one.
    for (_, newer, _), (line_number, older, _) in zip(readable, readable[1:], strict=False):
        if version_key(newer) <= version_key(older):
            problems.append(
                f"CHANGELOG.md:{line_number}: {older} is not older than {newer} above it — "
                f"sections run newest first"
            )

    if tag is None:
        return problems

    if not tag.startswith("v"):
        problems.append(f"tag {tag!r} does not start with 'v' (release.yml triggers on v*)")
    elif tag[1:] != version:
        problems.append(
            f"tag {tag!r} would publish {version!r} — the tag and pyproject.toml must name "
            f"the same version, or the wrong number goes to PyPI permanently"
        )

    if unreleased_has_content:
        problems.append(
            f"CHANGELOG.md:{unreleased_line}: 'Unreleased' still has entries — they would ship "
            f"in {version} without appearing in its release notes. Rename that heading to "
            f"'## {version} - <today>'."
        )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that the version agrees everywhere.")
    parser.add_argument("--tag", help="the git tag being released, e.g. v1.1.0")
    parser.add_argument("--root", help="repository root (default: this file's repo)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else REPO_ROOT
    problems = check(args.tag, root)
    if problems:
        print("Release check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    version = declared_version(root)
    where = "tag, " if args.tag else ""
    print(f"Release check OK: {where}pyproject.toml and CHANGELOG.md all say {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
