"""The package is `alhazen-vision` on pip and `alhazen` on import.

There is an unrelated project called `alhazen` on PyPI — a cognitive-modelling
framework from CMU, which has held the name since long before this one — and
that is not a hypothetical clash. Installing an experiment package that
declared a bare `alhazen` dependency into a clean environment really does
fetch it, and nothing then says so out loud:

- imports fail with errors that describe the other project's internals;
- worse, they do not always fail. ``get_version`` looks the distribution up by
  name, so with the wrong one installed it returns *their* version number,
  and that number is stamped into the manifest of every run this writes.

A developer machine never sees any of it, because the right package is
already installed and pip leaves a satisfied requirement alone. So these are
the tests that notice.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest

from alhazen.version import DISTRIBUTION, get_version

ROOT = Path(__file__).resolve().parents[2]

# Docs and templates that tell somebody what to type. A bare `pip install
# alhazen` in any of them installs the wrong project.
INSTALL_DOCS = (
    "README.md",
    "docs/getting-started.md",
    "docs/how-to.md",
    "src/alhazen/_scaffold/template/pyproject.toml.template",
)

# `alhazen` as a pip name, not followed by the suffix that makes it ours.
BARE_NAME = re.compile(r"""(?:pip install|dependencies\s*=)[^\n]*?["'\[\s]alhazen(?!-vision)\b""")


class TestWhatIsActuallyInstalled:
    def test_the_alhazen_import_is_provided_by_this_distribution(self):
        """If this fails, something else is providing `import alhazen`."""
        providers = set(metadata.packages_distributions().get("alhazen", []))

        assert providers == {DISTRIBUTION}, (
            f"`import alhazen` is provided by {sorted(providers)}, not just {DISTRIBUTION}. "
            f"PyPI's `alhazen` is an unrelated project; if it is installed here, every "
            f"version number this stamps into data is theirs."
        )

    def test_the_version_reported_is_this_distributions_own(self):
        assert get_version() == metadata.version(DISTRIBUTION)

    def test_the_version_is_known(self):
        """`unknown` means the distribution name was not found — which is what
        a rename that missed `version.py` looks like, and it is silent."""
        assert get_version() != "unknown"


class TestWhatTheDocsTellPeopleToType:
    @pytest.mark.parametrize("path", INSTALL_DOCS)
    def test_no_document_tells_anyone_to_install_the_bare_name(self, path):
        text = (ROOT / path).read_text()
        offenders = [line.strip() for line in text.splitlines() if BARE_NAME.search(line)]

        assert not offenders, (
            f"{path} tells the reader to install `alhazen`, which is a different "
            f"project on PyPI. Use `{DISTRIBUTION}`:\n  " + "\n  ".join(offenders)
        )

    def test_the_scaffold_gives_new_experiments_the_right_dependency(self):
        """The one that propagates: every experiment `alhazen new` creates
        inherits this line, so a mistake here is a mistake in every future
        repo rather than in one."""
        template = (ROOT / "src/alhazen/_scaffold/template/pyproject.toml.template").read_text()

        assert f'dependencies = ["{DISTRIBUTION}"]' in template
        assert f'"{DISTRIBUTION}[psychopy]"' in template
