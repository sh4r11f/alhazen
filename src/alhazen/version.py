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


__version__ = get_version()
