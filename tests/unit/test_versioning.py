"""One version number, in three places, that cannot disagree.

The number appears in `pyproject.toml` (declared), in `CHANGELOG.md` (what a
reader is told), and on a git tag (what CI builds from). A published number is
spent forever — PyPI refuses a reupload — so the cost of the three drifting
apart is not a broken build, it is a permanently wrong release.

These tests pin both halves of that: the version the running package reports,
and `scripts/release_check.py`, the gate `.github/workflows/release.yml` runs
before it builds anything. The check runs here on every commit as well, so
pyproject and the changelog cannot drift apart between releases either.

The number also appears in every deprecation warning, as the release that
removes the name (`removed_in`). That promise is checked against the declared
version too: `pause_menu` told its callers "will be removed in 1.2" from 1.1
through 1.5, because nothing compared the two.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import pytest

import alhazen
from alhazen.cli.main import main
from alhazen.version import DISTRIBUTION, get_version

REPO_ROOT = Path(__file__).parents[2]


def _load_release_check():
    """Import scripts/release_check.py by path.

    It lives outside the package on purpose — it is a repo tool, not something
    an experimenter installs — so there is no import path to it.
    """
    path = REPO_ROOT / "scripts" / "release_check.py"
    spec = importlib.util.spec_from_file_location("release_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_check = _load_release_check()


def write_repo(root: Path, version: str, changelog: str) -> Path:
    """A throwaway repo holding just the two files the check reads."""
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "alhazen"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return root


SOURCE_ROOT = REPO_ROOT / "src" / "alhazen"

# The two helpers in alhazen._deprecation that promise a removal version, and
# the position `removed_in` takes when it is passed positionally:
# deprecated(since, removed_in, instead) and
# warn_deprecated_argument(name, since, removed_in, instead).
REMOVED_IN_POSITION = {"deprecated": 1, "warn_deprecated_argument": 2}

# `removed_in` as the decorator is written: "2.0", or "2.0.0" in full.
REMOVED_IN_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?$")


@dataclass(frozen=True)
class Deprecation:
    """One call to a deprecation helper, as written in the source.

    ``removed_in`` is None when the call has no string literal there — a
    variable, an f-string, or a ``deprecated`` from somewhere else that takes
    no removal version at all. Such a call is reported, never skipped: a
    removal version this test cannot read is one it cannot check.
    """

    where: str
    removed_in: str | None


def find_deprecations(source: str, filename: str) -> list[Deprecation]:
    """Every call to a deprecation helper in one module's source, in line order.

    Read from the syntax tree rather than by importing the package and
    inspecting what it holds. The decorator records nothing on the function
    it wraps that could be read back reliably (only prose appended to the
    docstring), a deprecated method sits inside a class that an import walk
    would have to open, and `warn_deprecated_argument` runs inside a function
    body, where no import can see it at all. The source shows all three.

    A call is matched by the name it is made through, so both `deprecated(...)`
    and `_deprecation.deprecated(...)` count, and so does a local alias from
    `from alhazen._deprecation import deprecated as ...`. Matching by name
    over-matches on purpose: a `deprecated` from another library (such as
    `warnings.deprecated`) has no `removed_in`, so it is reported rather than
    silently passed — and a deprecation in alhazen that names no removal
    version is outside the policy anyway (docs/versioning.md §4).
    """
    tree = ast.parse(source, filename=filename)

    # The helpers' own names, plus any alias this module imports them under.
    positions = dict(REMOVED_IN_POSITION)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alhazen._deprecation":
            for alias in node.names:
                if alias.asname and alias.name in REMOVED_IN_POSITION:
                    positions[alias.asname] = REMOVED_IN_POSITION[alias.name]

    found: list[tuple[int, Deprecation]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called = node.func.id
        elif isinstance(node.func, ast.Attribute):
            called = node.func.attr
        else:
            continue  # a call through a subscript or another call's result
        if called not in positions:
            continue
        # `removed_in=` by keyword, else the positional slot it occupies.
        argument = next((kw.value for kw in node.keywords if kw.arg == "removed_in"), None)
        if argument is None and len(node.args) > positions[called]:
            argument = node.args[positions[called]]
        literal = (
            argument.value
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            else None
        )
        found.append((node.lineno, Deprecation(f"{filename}:{node.lineno}", literal)))
    # ast.walk is breadth-first; sort so messages read top to bottom.
    return [deprecation for _, deprecation in sorted(found, key=lambda item: item[0])]


def package_deprecations() -> list[Deprecation]:
    """Every deprecation in src/alhazen, named by its path under the package."""
    files = sorted(SOURCE_ROOT.rglob("*.py"))
    # A wrong root would find no files, and a scan of nothing passes every
    # check below — so an empty scan is a failure, not a clean result.
    assert files, f"no Python files under {SOURCE_ROOT}; the deprecation scan would be vacuous"
    found: list[Deprecation] = []
    for path in files:
        name = path.relative_to(SOURCE_ROOT).as_posix()
        found += find_deprecations(path.read_text(encoding="utf-8"), name)
    return found


def release_numbers(text: str | None) -> tuple[int, int, int] | None:
    """`removed_in` as (MAJOR, MINOR, PATCH), "2.0" meaning 2.0.0; None if it
    is not a version. TestDeprecationsPromiseAFutureMajor reports the Nones."""
    match = REMOVED_IN_RE.match(text) if text is not None else None
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


class TestTheRunningVersion:
    def test_one_source_of_truth(self):
        # `alhazen.__version__`, `get_version()` and the installed metadata are
        # the same lookup, so they cannot report different numbers.
        #
        # DISTRIBUTION rather than the literal "alhazen": the distribution is
        # `alhazen-vision`, because PyPI's `alhazen` is an unrelated project.
        # Naming it here again is how the two drift apart — and looking up the
        # wrong one does not raise, it silently returns the other project's
        # version (see test_distribution_identity.py).
        assert alhazen.__version__ == get_version() == metadata.version(DISTRIBUTION)

    def test_the_cli_reports_it_too(self, capsys):
        # argparse's version action exits; that is the success path.
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])
        assert exit_info.value.code == 0
        assert capsys.readouterr().out.strip() == f"alhazen {get_version()}"

    def test_an_uninstalled_source_tree_says_so(self, monkeypatch):
        # "unknown" rather than a guessed number: this string is stamped into
        # config snapshots and results manifests, and a wrong number there
        # misattributes someone's data to a version that never produced it.
        def missing(_name):
            raise metadata.PackageNotFoundError

        monkeypatch.setattr(metadata, "version", missing)
        assert get_version() == "unknown"

    def test_the_declared_version_is_semver(self):
        assert release_check.SEMVER_RE.match(release_check.declared_version(REPO_ROOT))


class TestThisRepoIsConsistent:
    def test_pyproject_and_the_changelog_agree(self):
        # The always-on half of the gate, run against the real files.
        assert release_check.check(tag=None, root=REPO_ROOT) == []

    def test_the_changelog_documents_the_installed_version(self):
        # The check above compares the changelog against pyproject. This one
        # compares it against what is actually installed, which is what a user
        # holds when they go looking for the release notes.
        version = get_version()
        if version == "unknown":
            pytest.skip("alhazen is not installed in this environment")
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert re.search(
            rf"^## {re.escape(version)} [-–—] \d{{4}}-\d{{2}}-\d{{2}}$",
            changelog,
            re.MULTILINE,
        ), f"CHANGELOG.md has no dated '## {version}' section for the installed version"

    def test_the_changelog_does_not_still_claim_to_be_pre_release(self):
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "pre-1.0" not in changelog

    def test_its_own_tag_would_pass_apart_from_folding_in_unreleased(self):
        """Everything the release-day gate checks except the one thing that is
        supposed to be true mid-development.

        `Unreleased` is where changes wait between landing and shipping, so it
        is non-empty most of the time and cutting the release is what empties
        it — asserting the whole gate here would assert that the repo is never
        mid-development, and forbid the workflow the changelog documents. That
        rule is exercised against a synthetic changelog in
        TestTheGateCatchesDrift, and `release_check.py --tag` still enforces it
        on the day. What is worth checking on every commit is all the rest:
        that a tag naming the declared version would trip nothing else.
        """
        version = release_check.declared_version(REPO_ROOT)
        problems = release_check.check(tag=f"v{version}", root=REPO_ROOT)
        assert [p for p in problems if "'Unreleased' still has entries" not in p] == []


class TestTheGateCatchesDrift:
    def test_a_tag_naming_a_different_version(self, tmp_path):
        root = write_repo(tmp_path, "1.0.0", "# Changelog\n\n## 1.0.0 - 2026-08-27\n\nFirst.\n")
        # The failure this whole gate exists for: the tag says 1.1.0, the build
        # would publish 1.0.0, and the number is gone either way.
        problems = release_check.check(tag="v1.1.0", root=root)
        assert any("would publish '1.0.0'" in problem for problem in problems)

    def test_a_bumped_pyproject_with_a_stale_changelog(self, tmp_path):
        root = write_repo(tmp_path, "1.1.0", "# Changelog\n\n## 1.0.0 - 2026-08-27\n\nFirst.\n")
        problems = release_check.check(tag=None, root=root)
        assert any("bump both in the same commit" in problem for problem in problems)

    def test_a_release_heading_with_no_date(self, tmp_path):
        root = write_repo(tmp_path, "1.0.0", "# Changelog\n\n## 1.0.0\n\nFirst.\n")
        problems = release_check.check(tag=None, root=root)
        assert any("YYYY-MM-DD" in problem for problem in problems)

    def test_unreleased_entries_left_behind_at_release(self, tmp_path):
        root = write_repo(
            tmp_path,
            "1.1.0",
            "# Changelog\n\n## Unreleased\n\n- A fix nobody wrote up.\n\n"
            "## 1.1.0 - 2026-09-14\n\nSecond.\n",
        )
        # Those entries would ship inside 1.1.0 while its notes stay silent
        # about them. Only a release run cares; day-to-day this is normal.
        assert release_check.check(tag=None, root=root) == []
        problems = release_check.check(tag="v1.1.0", root=root)
        assert any("'Unreleased' still has entries" in problem for problem in problems)

    def test_an_empty_unreleased_section_is_fine(self, tmp_path):
        root = write_repo(
            tmp_path, "1.1.0", "# Changelog\n\n## Unreleased\n\n## 1.1.0 - 2026-09-14\n\nOK.\n"
        )
        assert release_check.check(tag="v1.1.0", root=root) == []

    def test_sections_out_of_order(self, tmp_path):
        root = write_repo(
            tmp_path,
            "1.1.0",
            "# Changelog\n\n## 1.1.0 - 2026-09-14\n\nB\n\n## 1.2.0 - 2026-09-01\n\nA\n",
        )
        problems = release_check.check(tag=None, root=root)
        assert any("sections run newest first" in problem for problem in problems)

    def test_a_tag_without_the_v_prefix(self, tmp_path):
        root = write_repo(tmp_path, "1.0.0", "# Changelog\n\n## 1.0.0 - 2026-08-27\n\nFirst.\n")
        problems = release_check.check(tag="1.0.0", root=root)
        assert any("does not start with 'v'" in problem for problem in problems)

    def test_a_prerelease_sorts_below_its_release(self, tmp_path):
        root = write_repo(
            tmp_path,
            "1.1.0",
            "# Changelog\n\n## 1.1.0 - 2026-09-14\n\nB\n\n## 1.1.0-rc1 - 2026-09-01\n\nA\n",
        )
        assert release_check.check(tag="v1.1.0", root=root) == []

    def test_a_malformed_declared_version(self, tmp_path):
        root = write_repo(tmp_path, "1.0", "# Changelog\n\n## 1.0 - 2026-08-27\n\nFirst.\n")
        problems = release_check.check(tag=None, root=root)
        assert any("MAJOR.MINOR.PATCH" in problem for problem in problems)


class TestTheCommandLine:
    def test_it_exits_zero_when_everything_agrees(self, capsys):
        assert release_check.main(["--root", str(REPO_ROOT)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_it_exits_nonzero_and_names_the_problem(self, tmp_path, capsys):
        root = write_repo(tmp_path, "1.0.0", "# Changelog\n\n## 1.0.0 - 2026-08-27\n\nFirst.\n")
        assert release_check.main(["--root", str(root), "--tag", "v9.9.9"]) == 1
        assert "FAILED" in capsys.readouterr().err


class TestDeprecationsPromiseAFutureMajor:
    """Every deprecation in the package names a removal the package has not
    reached, and that removal is a MAJOR release (docs/versioning.md §4).

    The first rule is the one that was broken: `pause_menu` said "will be
    removed in 1.2" to every caller from 1.1 through 1.5. A warning that
    names a release already out is false twice over — the name was not
    removed, and the reader is told they have already run out of time. The
    second rule is why it will not recur: removing a public name breaks its
    callers, which semantic versioning allows only in a MAJOR release.

    The version compared against is `pyproject.toml`'s, the source of truth,
    so bumping it to the release that removes a name fails here until the
    name is gone. Only MAJOR.MINOR.PATCH is compared: a 2.0.0-rc1 is already
    at 2.0, because a release candidate should be what 2.0.0 will be.
    """

    def test_every_removal_version_is_a_version_this_test_can_read(self):
        unreadable = [
            f"{deprecation.where}: removed_in={deprecation.removed_in!r}"
            for deprecation in package_deprecations()
            if release_numbers(deprecation.removed_in) is None
        ]
        assert unreadable == [], (
            "write removed_in as a string literal naming a MAJOR release, such as "
            "removed_in='2.0', using alhazen._deprecation's helpers — a removal "
            f"version this test cannot read is one it cannot check: {unreadable}"
        )

    def test_no_removal_version_has_already_been_reached(self):
        declared = release_check.declared_version(REPO_ROOT)
        current = release_check.version_key(declared)[:3]
        reached = [
            f"{deprecation.where}: removed_in={deprecation.removed_in!r}"
            for deprecation in package_deprecations()
            if (numbers := release_numbers(deprecation.removed_in)) is not None
            and numbers <= current
        ]
        assert reached == [], (
            f"pyproject.toml declares {declared}, but these deprecations still promise "
            f"a removal at or below it, so every call warns about a release that has "
            f"already happened. Delete the name if this is the MAJOR release that "
            f"removes it; otherwise move removed_in to the next MAJOR: {reached}"
        )

    def test_every_removal_is_a_major_release(self):
        not_major = [
            f"{deprecation.where}: removed_in={deprecation.removed_in!r}"
            for deprecation in package_deprecations()
            if (numbers := release_numbers(deprecation.removed_in)) is not None
            and numbers[1:] != (0, 0)
        ]
        assert not_major == [], (
            "a deprecated name is removed only in a MAJOR release (docs/versioning.md "
            f"§4), so removed_in names one, such as '2.0': {not_major}"
        )


class TestTheDeprecationScan:
    """The scan the tests above rely on. A scan that silently missed a form
    would pass them with a stale promise still in the source."""

    def test_it_reads_every_way_the_helpers_are_called(self):
        source = (
            "from alhazen import _deprecation\n"
            "from alhazen._deprecation import deprecated, warn_deprecated_argument\n"
            "from alhazen._deprecation import deprecated as going\n"
            "\n"
            "@deprecated(since='1.1', removed_in='1.2', instead='new')\n"
            "def by_keyword(): ...\n"
            "\n"
            "@deprecated('1.1', '1.3')\n"
            "def by_position(): ...\n"
            "\n"
            "class Holder:\n"
            "    @going(since='1.1', removed_in='1.4')\n"
            "    def through_an_alias(self): ...\n"
            "\n"
            "    @_deprecation.deprecated(since='1.1', removed_in='1.5')\n"
            "    def through_the_module(self): ...\n"
            "\n"
            "def one_argument(old=None):\n"
            "    if old is not None:\n"
            "        warn_deprecated_argument('old', '1.1', '1.6', 'new')\n"
        )
        found = find_deprecations(source, "fake.py")
        assert [(d.where, d.removed_in) for d in found] == [
            ("fake.py:5", "1.2"),
            ("fake.py:8", "1.3"),
            ("fake.py:12", "1.4"),
            ("fake.py:15", "1.5"),
            ("fake.py:20", "1.6"),
        ]

    def test_a_removal_version_it_cannot_read_is_reported_not_skipped(self):
        source = (
            "import warnings\n"
            "from alhazen._deprecation import deprecated\n"
            "LATER = '1.2'\n"
            "@deprecated(since='1.1', removed_in=LATER)\n"
            "def computed(): ...\n"
            "@warnings.deprecated('use something else')\n"
            "def another_librarys(): ...\n"
        )
        found = find_deprecations(source, "fake.py")
        assert [(d.where, d.removed_in) for d in found] == [
            ("fake.py:4", None),
            ("fake.py:6", None),
        ]
        assert [release_numbers(d.removed_in) for d in found] == [None, None]

    def test_short_and_full_versions_read_the_same(self):
        assert release_numbers("2.0") == release_numbers("2.0.0") == (2, 0, 0)
        assert release_numbers("2.0-rc1") is None
