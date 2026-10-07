"""The installed package version, in one place.

Its own module, outside the layering contract, so that any layer can stamp a
version into what it writes without importing the root package — which pulls
in the whole session stack. An analysis machine reading a results bundle
should not have to import a trial engine to learn what wrote it.
"""

from __future__ import annotations

from importlib import metadata

# The DISTRIBUTION name, which is not the import name. `pip install alhazen`
# installs an unrelated cognitive-modelling framework from CMU that has held
# that name on PyPI since long before this project; this one is published and
# depended on as `alhazen-vision` and imported as `alhazen`. Looking the wrong
# one up here does not fail — it silently returns the other project's version,
# which then gets stamped into the manifest of every run.
DISTRIBUTION = "alhazen-vision"


def get_version() -> str:
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        # Running from a source tree with nothing installed: honest about
        # not knowing, rather than inventing a number that would end up
        # stamped into someone's data.
        return "unknown"


def experiment_distribution_version(name: str) -> str:
    """The installed version of an experiment's distribution, by the exact
    name `importlib.metadata.packages_distributions()` gave for its package.

    Every version lookup in alhazen lives in this module, so the wrong-name
    trap above has one place to be checked (tests/unit/test_distribution_
    identity.py holds the rest of the package to that). This one is for the
    experiment, not for alhazen: `alhazen.config.experiment` falls back to it
    for a task installed from a wheel. The name is never typed by hand — it
    comes from the metadata that says which distribution ships the package —
    so it cannot be the look-alike that `DISTRIBUTION` guards against.
    Raises `importlib.metadata.PackageNotFoundError` for an unknown name:
    unlike alhazen's own version, an experiment's names a data folder, and
    "unknown" there would be invented.
    """
    return metadata.version(name)


def dependency_version(name: str) -> str | None:
    """The installed version of a library a run used (PsychoPy), for a
    report's provenance; None when it is not installed. Here with every other
    version lookup (tests/unit/test_distribution_identity.py)."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


__version__ = get_version()
