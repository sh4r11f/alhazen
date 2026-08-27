"""Every Python snippet in the documentation compiles.

A snippet that does not even parse is documentation that has never been read
by anything. The landing page's own example carried a `SyntaxError` for a
whole release — `...` inside a call's keyword arguments — and nothing
noticed, because the docs were prose to every tool in the repo.

Compilation, not execution: a snippet is written to be read, and most of them
name a task or a rig that only exists in the reader's own project. What can
be checked without inventing a context is that the code is Python.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).parents[2] / "docs"
# ```python … ``` — the only fence the docs use for code that must parse.
FENCE = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)

# A snippet may be a fragment on purpose (a class body, a config excerpt). It
# opts out by starting with this marker, which stays visible in the rendered
# page as an ordinary comment.
FRAGMENT_MARKER = "# fragment"


def snippets() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(DOCS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for match in FENCE.finditer(text):
            line = text[: match.start()].count("\n") + 1
            found.append((str(path.relative_to(DOCS)), line, match.group(1)))
    return found


ALL_SNIPPETS = snippets()


def test_the_docs_actually_contain_python_snippets():
    # A regex that silently matched nothing would make every test below pass.
    assert len(ALL_SNIPPETS) >= 5


@pytest.mark.parametrize(
    ("name", "line", "source"),
    ALL_SNIPPETS,
    ids=[f"{name}:{line}" for name, line, _ in ALL_SNIPPETS],
)
def test_every_snippet_parses(name, line, source):
    if source.lstrip().startswith(FRAGMENT_MARKER):
        pytest.skip("marked as a fragment")
    try:
        compile(source, f"docs/{name}:{line}", "exec")
    except SyntaxError as error:
        raise AssertionError(
            f"docs/{name} line {line}: the snippet is not valid Python — {error}"
        ) from error


# ::: alhazen.something — one mkdocstrings target per line.
DIRECTIVE = re.compile(r"^:::\s+(\S+)\s*$", re.MULTILINE)


def reference_targets() -> list[str]:
    return DIRECTIVE.findall((DOCS / "reference.md").read_text(encoding="utf-8"))


ALL_TARGETS = reference_targets()


def test_the_reference_page_documents_the_package():
    # A regex that silently matched nothing would make the test below pass.
    assert len(ALL_TARGETS) >= 20
    assert "alhazen" in ALL_TARGETS


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_every_reference_target_imports(target):
    """`mkdocs build --strict` fails on a target that no longer exists, but
    the docs build is not one of the five gates — so a module that moved
    would break the site with nothing in the suite noticing.
    """
    import importlib

    try:
        importlib.import_module(target)
    except ImportError as error:
        raise AssertionError(
            f"docs/reference.md documents {target!r}, which cannot be imported: {error}"
        ) from error
