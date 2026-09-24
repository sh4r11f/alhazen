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


# The public API is exactly the names reference.md lists (docs/versioning.md
# §1), so every entry must say which names those are. A bare `::: module` —
# or `members: true` — renders every public-looking name in the module, and
# that is how formatting helpers and tuning constants became contractually
# public, each refactor of them technically a MAJOR bump.
#
# One directive and its indented options block: the target on the `:::` line,
# then every following line that starts with whitespace. The block is YAML,
# parsed the way mkdocstrings parses it, so a list wrapped over several lines
# is read exactly as the docs build reads it.
DIRECTIVE_WITH_OPTIONS = re.compile(r"^:::[ \t]+(\S+)[ \t]*\n((?:[ \t]+\S.*\n)*)", re.MULTILINE)


def reference_members() -> dict[str, object]:
    """Each target's `members` option as written: a list, or whatever else
    (None when absent) — the test below says which targets are wrong."""
    import textwrap

    import yaml

    # A trailing newline so the last entry's options block still matches when
    # the file does not end in one.
    text = (DOCS / "reference.md").read_text(encoding="utf-8") + "\n"
    found: dict[str, object] = {}
    for match in DIRECTIVE_WITH_OPTIONS.finditer(text):
        target, block = match.group(1), match.group(2)
        options = (yaml.safe_load(textwrap.dedent(block)) or {}).get("options") if block else None
        found[target] = (options or {}).get("members")
    return found


ALL_MEMBERS = reference_members()


def test_the_member_parser_sees_every_target():
    # The two regexes must agree, or an entry the parser missed would escape
    # both tests below.
    assert sorted(ALL_MEMBERS) == sorted(ALL_TARGETS)


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_every_reference_entry_lists_its_public_names(target):
    members = ALL_MEMBERS[target]
    assert isinstance(members, list) and members, (
        f"docs/reference.md documents {target!r} without an explicit `members: [...]` "
        f"list (got {members!r}), which makes every name in the module public API. "
        f"List the names experiments use; the rest stay internal."
    )
    duplicates = sorted({name for name in members if members.count(name) > 1})
    assert not duplicates, f"docs/reference.md lists {duplicates} twice under {target!r}"


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_every_listed_member_exists(target):
    """A listed name that no longer exists is a public name that was renamed
    or removed — which the version number has to say, and this page too."""
    import importlib

    module = importlib.import_module(target)
    members = ALL_MEMBERS[target]
    missing = [name for name in members if not hasattr(module, name)] if members else []
    assert not missing, (
        f"docs/reference.md lists {missing} under {target!r}, but the module has no such "
        f"names. A public name was renamed or removed: fix the list, and if it was public, "
        f"deprecate it first (docs/versioning.md §4) rather than drop it."
    )


def test_the_top_level_entry_is_exactly_alhazen_all():
    # `alhazen.__all__` is public by definition (versioning.md §1); the page
    # must show all of it and nothing else, so a name added to or removed
    # from `__all__` cannot leave the reference behind.
    import alhazen

    assert set(ALL_MEMBERS["alhazen"]) == set(alhazen.__all__) | {"__version__"}
