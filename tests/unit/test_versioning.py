"""One version number, in three places, that cannot disagree.

The number appears in `pyproject.toml` (declared), in `CHANGELOG.md` (what a
reader is told), and on a git tag (what CI builds from). A published number is
spent forever — PyPI refuses a reupload — so the cost of the three drifting
apart is not a broken build, it is a permanently wrong release.

These tests pin both halves of that: the version the running package reports,
and `scripts/release_check.py`, the gate `.github/workflows/release.yml` runs
before it builds anything. The check runs here on every commit as well, so
pyproject and the changelog cannot drift apart between releases either.
"""

from __future__ import annotations

import importlib.util
import re
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
