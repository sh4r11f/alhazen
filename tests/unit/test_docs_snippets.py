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


def subpackages_with_all() -> list[str]:
    """Every package under `alhazen` (nested ones too) that declares `__all__`."""
    import importlib
    import pkgutil

    import alhazen

    found = []
    for info in pkgutil.walk_packages(alhazen.__path__, "alhazen."):
        if info.ispkg and hasattr(importlib.import_module(info.name), "__all__"):
            found.append(info.name)
    return found


SUBPACKAGES_WITH_ALL = subpackages_with_all()


def test_the_subpackage_scan_finds_the_subpackages():
    # A walk that silently found nothing would make the test below pass.
    assert {"alhazen.core", "alhazen.devices", "alhazen.scenes"} <= set(SUBPACKAGES_WITH_ALL)


@pytest.mark.parametrize("package", SUBPACKAGES_WITH_ALL)
def test_a_subpackage_all_exports_only_names_the_reference_lists(package):
    """`__all__` is what `import *` hands out and what editors and linters
    present as a package's API, so a name in it reads as public whatever the
    reference page says. The page says a subpackage's `__all__` does not make
    a name public; this keeps `__all__` from contradicting it.

    A name counts as listed when the page lists it under the subpackage itself
    or under one of its modules — the ones a re-export for a shorter import
    (`from alhazen.scenes import load_scene`) comes from. An internal name the
    subpackage still imports stays importable from it; it is just not
    exported.
    """
    import importlib

    listed = {
        name
        for target, members in ALL_MEMBERS.items()
        if target == package or target.startswith(package + ".")
        for name in members or []
    }
    unlisted = sorted(set(importlib.import_module(package).__all__) - listed)
    assert not unlisted, (
        f"{package}.__all__ exports {unlisted}, which docs/reference.md does not list under "
        f"{package} or any of its modules. Either the name is public — list it on the "
        f"reference page — or it is internal: take it out of `__all__` (keep the import as "
        f"`import X as X` if anything imports it from {package})."
    )


# The names that left the subpackages' `__all__` when it was brought in line
# with the reference page. Leaving `__all__` changes only `import *`; code that
# imports one of these from the subpackage by name keeps working, and this
# pins that, so tidying an `__init__` cannot quietly break an experiment.
FORMERLY_EXPORTED = {
    "alhazen.analysis": ["PhotodiodeReport", "SessionReport", "build_report"],
    "alhazen.cli": ["main"],
    "alhazen.config": ["resolve_refresh", "write_snapshot"],
    "alhazen.core": ["KeyboardCommands", "NullCommands", "resolve_seed"],
    "alhazen.data": ["SessionPaths", "ensure_participant", "participants_path"],
    "alhazen.devices": [
        "EyeLinkTracker",
        "MouseSimTracker",
        "NidaqReward",
        "NidaqSync",
        "NullResponse",
        "NullSync",
        "ScriptedTracker",
        "SubjectKeyboard",
        "ViewPixxTracker",
        "build_reward_waveform",
        "make_reward",
        "make_sync",
        "make_sync_subscriber",
        "make_tracker",
    ],
    "alhazen.devices.eyetracker": [
        "EyeLinkTracker",
        "MouseSimTracker",
        "ProgressHook",
        "ScriptedTracker",
        "ViewPixxTracker",
        "is_missing_gaze",
        "make_tracker",
    ],
    "alhazen.display": ["Registration", "within_radius"],
    "alhazen.modes": ["MODE_SUMMARIES", "flag_refusal"],
    "alhazen.neural": ["SpikeDetector", "StreamTimebase"],
    "alhazen.paradigms": ["QuestPlusEstimator", "make_scheduler", "weibull"],
    "alhazen.scenes": [
        "EvalContext",
        "RenderContext",
        "SUPPORTED_PRIMITIVES",
        "SUPPORTED_VERSION",
        "compile_expr",
        "evaluate_expr",
        "mulberry32",
    ],
    "alhazen.session": ["TrialPlan", "TrialSetup"],
    "alhazen.stimuli": ["FixationPoint", "PhotodiodePatch", "make_photodiode"],
    "alhazen.task": ["SubjectMode", "response_phases"],
    "alhazen.training": ["StageChange", "TrainingState", "TrainingSupervisor"],
}


@pytest.mark.parametrize("package", sorted(FORMERLY_EXPORTED))
def test_names_taken_out_of_all_are_still_importable(package):
    import importlib

    module = importlib.import_module(package)
    missing = [name for name in FORMERLY_EXPORTED[package] if not hasattr(module, name)]
    assert not missing, (
        f"`from {package} import ...` no longer finds {missing}. They left `__all__` "
        f"but must stay importable from {package}: code outside this repo imports them "
        f"that way. Keep the import in {package}/__init__.py (as `import X as X`)."
    )
