"""The template ``alhazen new`` renders into a new experiment package.

Kept as files under ``template/`` with ``$placeholders`` rather than as
strings in code: a template you can open and read is a template someone will
keep current, and the rendered result is exactly the file that was reviewed.

Rendering is stdlib ``string.Template`` — deliberately not a templating
library. A scaffold that needed a dependency to run would be one more thing
between a new user and their first session.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from string import Template

from alhazen.errors import ConfigError

log = logging.getLogger(__name__)

TEMPLATE_ROOT = Path(__file__).parent / "template"

# A package name has to be an importable module AND a filename segment: a
# task named "My Task" would produce a package nobody can import.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def python_name(name: str) -> str:
    """The importable form of an experiment's name."""
    return name.replace("-", "_")


def task_class_name(name: str) -> str:
    """``motion_discrimination`` -> ``MotionDiscriminationTask``."""
    return "".join(part.capitalize() for part in python_name(name).split("_")) + "Task"


def scaffold(name: str, destination: Path, force: bool = False) -> Path:
    """Write a new experiment package, and return its directory.

    Refuses to write into a directory that already has files, unless told to:
    a scaffold that overwrote someone's work would be a worse first
    impression than one that stopped.
    """
    package = python_name(name)
    if not NAME_PATTERN.match(package):
        raise ConfigError(
            f"{name!r} cannot be a package name: use lowercase letters, digits and "
            f"underscores or hyphens, starting with a letter (e.g. 'saccade_bias')"
        )

    root = Path(destination) / name
    if root.exists() and any(root.iterdir()) and not force:
        raise ConfigError(
            f"{root} already exists and is not empty. Pick another name, or pass "
            f"--force if you meant to write into it."
        )

    substitutions = {
        "name": name,
        "package": package,
        "task_class": task_class_name(name),
        "task_name": name.replace("_", "-"),
    }

    for source in sorted(TEMPLATE_ROOT.rglob("*")):
        if source.is_dir():
            continue
        # A template path may itself contain a placeholder — that is how
        # src/$package/ becomes src/saccade_bias/.
        relative = Template(str(source.relative_to(TEMPLATE_ROOT))).substitute(substitutions)
        # ".py.template" keeps the templates out of the package's own import
        # path and off ruff's radar; the rendered file is a real .py.
        target = root / relative.removesuffix(".template")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(Template(source.read_text()).substitute(substitutions))
        log.info("wrote %s", target.relative_to(root))

    return root
