"""The layering contract covers every package, and the docs draw it as it is.

`lint-imports` enforces the layers in pyproject's `[tool.importlinter]` — but
only for the packages that list names. A package left off it is not checked
at all, in either direction: `alhazen.modes` imported `alhazen.cli.calibrate`
while `alhazen.cli` imported `alhazen.modes`, and the gate stayed green
through that cycle because nothing had told it where modes sits.
CONTRIBUTING.md says a new package joins the list in the same change that
adds it; this is the test that notices when one does not.

The same list is drawn twice more for people: the diagram in CONTRIBUTING.md
and the sentence in docs/architecture.md §1. Both had drifted from the config
(neither showed modes; CONTRIBUTING also lacked live monitor and neural), and a
drawing that disagrees with the gate teaches the wrong layering to whoever
reads it first — so the drawings are checked against the config too.
"""

from __future__ import annotations

import re
from pathlib import Path

try:  # tomllib is stdlib from 3.11; tomli is the dev-extra backport for 3.10.
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "alhazen"


def _line(text: str) -> list[str]:
    """One layer line as its package names: ``"alhazen.session | alhazen.testing"``
    (or the docs' ``session | testing``) -> ``["session", "testing"]``.

    Sorted, because order within a line means nothing to the contract (the
    packages on one line are only required to be independent of each other),
    while the order of the lines is the whole point and is kept by the callers.
    """
    return sorted(name.strip().removeprefix("alhazen.") for name in text.split("|"))


def contract_layers() -> list[list[str]]:
    """The layers contract lint-imports enforces, top line first."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    layered = [contract for contract in contracts if contract["type"] == "layers"]
    # One layers contract is what CONTRIBUTING and architecture.md describe; a
    # second one would need its own drawing, and this test would need telling.
    assert len(layered) == 1, f"expected one layers contract in pyproject, found {len(layered)}"
    return [_line(layer) for layer in layered[0]["layers"]]


def contributing_layers() -> list[list[str]]:
    """The diagram under CONTRIBUTING.md's "The layering contract", top line first."""
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    match = re.search(r"Imports point only downward:\s*```\n(.*?)```", text, re.DOTALL)
    assert match, "CONTRIBUTING.md has no fenced diagram after 'Imports point only downward:'"
    # The diagram wraps across lines; splitting on the arrows makes the line
    # breaks and indentation irrelevant.
    return [_line(part) for part in match.group(1).split("→")]


def architecture_layers() -> list[list[str]]:
    """The layering sentence in docs/architecture.md §1, top line first."""
    text = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    # \s+ between the words: the sentence is hard-wrapped, and where its line
    # breaks fall moves whenever the sentence is edited.
    match = re.search(r"top to bottom:(.*?)\.\s+Imports\s+point\s+only\s+downward", text, re.DOTALL)
    assert match, (
        "docs/architecture.md has no 'top to bottom: ... Imports point only downward' "
        "sentence to check against the layering contract"
    )
    # Each layer line is one `code span`; the arrows between them are prose.
    return [_line(part) for part in re.findall(r"`([^`]+)`", match.group(1))]


class TestTheContractCoversEveryPackage:
    def test_every_subpackage_of_alhazen_is_on_a_layer(self):
        """A package off the list is invisible to lint-imports: nothing stops it
        importing upward, and nothing stops a package above it depending on it
        in a cycle. (Single modules such as errors.py and version.py sit
        outside the contract on purpose; only packages are required here.)"""
        listed = {name for line in contract_layers() for name in line}
        packages = {path.name for path in PACKAGE.iterdir() if (path / "__init__.py").is_file()}

        missing = sorted(packages - listed)
        assert not missing, (
            f"alhazen packages missing from the layers contract: {missing}. lint-imports "
            f"checks nothing about a package it is not told about — add each one to "
            f"[tool.importlinter] in pyproject.toml on the line its imports put it, and "
            f"to the two drawings of the contract (CONTRIBUTING.md, docs/architecture.md)."
        )


class TestTheDocsDrawTheContract:
    def test_contributing_draws_the_contract(self):
        assert contributing_layers() == contract_layers(), (
            "CONTRIBUTING.md's layering diagram disagrees with pyproject's "
            "[tool.importlinter] layers — redraw it from the config: its lines, top to "
            "bottom, joined by →"
        )

    def test_architecture_draws_the_contract(self):
        assert architecture_layers() == contract_layers(), (
            "docs/architecture.md §1's layering sentence disagrees with pyproject's "
            "[tool.importlinter] layers — rewrite it from the config: its lines, top to "
            "bottom, one `code span` each, joined by →"
        )
